from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import aliased

from app.devices.models import DeviceIntent, DeviceRemediationLogEntry, DeviceReservation, ExclusionKind
from app.devices.services.claims import reservation_active
from app.devices.services.decision import DecisionFacts
from app.devices.services.health_view import device_allows_allocation
from app.devices.services.intent_types import VERIFICATION_OUTCOME_KEY, verification_intent_source
from app.devices.services.readiness import assess_device_with_pack, load_packs_by_ids
from app.devices.services.state import DeviceStateFacts, WithdrawalFacts, appium_node_stop_in_flight
from app.lifecycle.services import remediation_log
from app.sessions.live_session_predicate import live_session_predicate, masking_live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.locking import LockedDevice
    from app.lifecycle.services.remediation_log import LadderState
    from app.packs.models import DriverPack


@dataclass(frozen=True, slots=True)
class IntentSnapshot:
    id: uuid.UUID
    device_id: uuid.UUID
    source: str
    kind: str
    run_id: uuid.UUID | None
    payload: dict[str, Any]
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReservationDecisionSnapshot:
    run_id: uuid.UUID
    exclusion_kind: str | None
    exclusion_reason: str | None
    excluded_until: datetime | None


@dataclass(frozen=True, slots=True)
class DeviceDecisionSnapshot:
    intents: tuple[IntentSnapshot, ...]
    has_live_session: bool
    ladder: LadderState
    decision_facts: DecisionFacts
    state_facts: DeviceStateFacts
    host_ip: str | None
    host_agent_port: int | None
    node_observed_pack_release: str | None
    node_port: int | None


def _uuid(value: object) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _optional_uuid(value: object) -> uuid.UUID | None:
    return None if value is None else _uuid(value)


def _optional_datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _intent_snapshots(raw: object) -> tuple[IntentSnapshot, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(
        IntentSnapshot(
            id=_uuid(row["id"]),
            device_id=_uuid(row["device_id"]),
            source=str(row["source"]),
            kind=str(row["kind"]),
            run_id=_optional_uuid(row.get("run_id")),
            payload=dict(row.get("payload") or {}),
            expires_at=_optional_datetime(row.get("expires_at")),
        )
        for row in raw
        if isinstance(row, dict)
    )


def _reservation_snapshot(raw: object) -> ReservationDecisionSnapshot | None:
    if not isinstance(raw, dict):
        return None
    return ReservationDecisionSnapshot(
        run_id=_uuid(raw["run_id"]),
        exclusion_kind=str(raw["exclusion_kind"]) if raw.get("exclusion_kind") is not None else None,
        exclusion_reason=str(raw["exclusion_reason"]) if raw.get("exclusion_reason") is not None else None,
        excluded_until=_optional_datetime(raw.get("excluded_until")),
    )


async def _load_claims_intents_and_reservation(
    db: AsyncSession,
    device_id: uuid.UUID,
) -> tuple[tuple[IntentSnapshot, ...], bool, bool, ReservationDecisionSnapshot | None]:
    intent_rows = (
        select(
            DeviceIntent.id.label("id"),
            DeviceIntent.device_id.label("device_id"),
            DeviceIntent.source.label("source"),
            DeviceIntent.kind.label("kind"),
            DeviceIntent.run_id.label("run_id"),
            DeviceIntent.payload.label("payload"),
            DeviceIntent.expires_at.label("expires_at"),
        )
        .where(DeviceIntent.device_id == device_id)
        .subquery()
    )
    intent_object = func.jsonb_build_object(
        "id",
        intent_rows.c.id,
        "device_id",
        intent_rows.c.device_id,
        "source",
        intent_rows.c.source,
        "kind",
        intent_rows.c.kind,
        "run_id",
        intent_rows.c.run_id,
        "payload",
        intent_rows.c.payload,
        "expires_at",
        intent_rows.c.expires_at,
    )
    intents = (
        select(func.jsonb_agg(aggregate_order_by(intent_object, intent_rows.c.source)))
        .select_from(intent_rows)
        .scalar_subquery()
    )
    reservation = (
        select(
            func.jsonb_build_object(
                "run_id",
                DeviceReservation.run_id,
                "exclusion_kind",
                DeviceReservation.exclusion_kind,
                "exclusion_reason",
                DeviceReservation.exclusion_reason,
                "excluded_until",
                DeviceReservation.excluded_until,
            )
        )
        .where(DeviceReservation.device_id == device_id, reservation_active())
        .order_by(DeviceReservation.created_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    live = exists(select(Session.id).where(live_session_predicate(device_id)))
    masking = exists(select(Session.id).where(masking_live_session_predicate(device_id)))
    raw_intents, has_live, has_masking, raw_reservation = (
        await db.execute(select(intents, live, masking, reservation))
    ).one()
    return (
        _intent_snapshots(raw_intents),
        bool(has_live),
        bool(has_masking),
        _reservation_snapshot(raw_reservation),
    )


async def _load_current_ladder(db: AsyncSession, device_id: uuid.UUID) -> LadderState:
    reset = aliased(DeviceRemediationLogEntry)
    latest_reset_at = (
        select(reset.at)
        .where(reset.device_id == device_id, reset.kind == remediation_log.KIND_RESET)
        .order_by(reset.at.desc(), reset.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    latest_reset_id = (
        select(reset.id)
        .where(reset.device_id == device_id, reset.kind == remediation_log.KIND_RESET)
        .order_by(reset.at.desc(), reset.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    entries = list(
        (
            await db.execute(
                select(DeviceRemediationLogEntry)
                .where(
                    DeviceRemediationLogEntry.device_id == device_id,
                    or_(
                        latest_reset_at.is_(None),
                        DeviceRemediationLogEntry.at > latest_reset_at,
                        and_(
                            DeviceRemediationLogEntry.at == latest_reset_at,
                            DeviceRemediationLogEntry.id >= latest_reset_id,
                        ),
                    ),
                )
                .order_by(DeviceRemediationLogEntry.at, DeviceRemediationLogEntry.id)
            )
        )
        .scalars()
        .all()
    )
    return remediation_log.derive_ladder(entries)


async def load_device_decision_snapshot(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    packs: Mapping[str, DriverPack],
    now: datetime,
) -> DeviceDecisionSnapshot:
    locked.assert_active(db)
    device = locked.device
    intents, has_live, has_masking, reservation = await _load_claims_intents_and_reservation(db, device.id)
    ladder = await _load_current_ladder(db, device.id)
    pack = packs.get(device.pack_id)
    if pack is None:
        pack = (await load_packs_by_ids(db, [device.pack_id])).get(device.pack_id)
    withdrawal = WithdrawalFacts.from_device(device)
    ready = (
        assess_device_with_pack(device, pack).readiness_state == "verified"
        and device_allows_allocation(device)
        and withdrawal.in_service()
    )
    reservation_run_id = None
    cooldown_active = False
    cooldown_reason = None
    if reservation is not None and reservation.exclusion_kind != ExclusionKind.exclusion:
        reservation_run_id = reservation.run_id
        if (
            reservation.exclusion_kind == ExclusionKind.cooldown
            and reservation.excluded_until is not None
            and reservation.excluded_until > now
        ):
            cooldown_active = True
            cooldown_reason = reservation.exclusion_reason

    return DeviceDecisionSnapshot(
        intents=intents,
        has_live_session=has_live,
        ladder=ladder,
        decision_facts=DecisionFacts(
            in_maintenance=withdrawal.in_maintenance,
            device_checks_unhealthy=device.device_checks_healthy is False,
            in_service=withdrawal.in_service(),
            reservation_run_id=reservation_run_id,
            cooldown_active=cooldown_active,
            cooldown_reason=cooldown_reason,
            remediation_directive=ladder.node_directive,
        ),
        state_facts=DeviceStateFacts(
            has_running_session=has_masking,
            has_verification_lease=any(
                intent.source == verification_intent_source(device.id)
                and intent.payload.get(VERIFICATION_OUTCOME_KEY) is None
                and (intent.expires_at is None or intent.expires_at > now)
                for intent in intents
            ),
            in_maintenance=withdrawal.in_maintenance,
            stop_in_flight=appium_node_stop_in_flight(device),
            ready=ready,
        ),
        host_ip=device.host.ip if device.host is not None else None,
        host_agent_port=device.host.agent_port if device.host is not None else None,
        node_observed_pack_release=(
            device.appium_node.observed_pack_release if device.appium_node is not None else None
        ),
        node_port=device.appium_node.port if device.appium_node is not None else None,
    )
