"""Batch, immutable device read projection.

Composes the existing batch primitives (readiness, operational state,
reservation, ladder, platform label, static group keys, decision facts,
recovery availability) into one bounded load per read. Every DB call happens
outside the per-device loop; the per-device step only assembles already-loaded
facts into a frozen :class:`DeviceReadProjection`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.devices.models import DeviceIntent
from app.devices.services.decision import DecisionFacts, parse_command, reservation_decision_axes
from app.devices.services.group_membership import load_static_group_keys_by_device_id
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.devices.services.lifecycle_policy_summary import freeze_reservation_context
from app.devices.services.platform_label import platform_labels_from_catalog
from app.devices.services.readiness import assess_devices_async
from app.devices.services.recovery_projection import recovery_availability_from_facts
from app.devices.services.serialization_types import DeviceReadProjection
from app.devices.services.state import WithdrawalFacts, derive_operational_states
from app.lifecycle.services import remediation_log
from app.packs.services import platform_resolver as pack_platform_resolver
from app.packs.services.catalog_view import load_pack_catalog
from app.runs.service_reservation import get_device_reservation_map, get_reservation_context_for_device
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device, DeviceOperationalState
    from app.devices.services.readiness import DeviceReadiness
    from app.devices.services.serialization_types import ReservationReadFacts
    from app.lifecycle.services.remediation_log import LadderState
    from app.packs.services.catalog_view import PackView


async def load_device_read_projections(
    db: AsyncSession,
    devices: Sequence[Device],
    *,
    now: datetime,
) -> Mapping[UUID, DeviceReadProjection]:
    device_list = list(devices)
    if not device_list:
        return {}
    device_ids = [device.id for device in device_list]
    packs = await load_pack_catalog(db, {device.pack_id for device in device_list if device.pack_id})
    readiness = await assess_devices_async(db, device_list, packs=packs)
    states = await derive_operational_states(db, device_list, now=now, packs=packs)
    reservation_map = await get_device_reservation_map(db, device_ids)
    ladders = await remediation_log.load_ladders(db, device_ids)
    labels = platform_labels_from_catalog(packs)
    static_keys = await load_static_group_keys_by_device_id(db, device_ids)
    intents = await _load_intents_by_device_id(db, device_ids)
    live_ids = await _load_live_session_device_ids(db, device_ids)

    _require_complete_batch(
        device_ids,
        ("readiness", readiness),
        ("operational_state", states),
        ("ladder", ladders),
    )

    return {
        device.id: _build_device_read_projection(
            device,
            readiness=readiness[device.id],
            operational_state=states[device.id],
            reservation=freeze_reservation_context(
                *get_reservation_context_for_device(reservation_map.get(device.id), device.id),
                device_id=device.id,
            ),
            ladder=ladders[device.id],
            intents=intents.get(device.id, ()),
            live_session=device.id in live_ids,
            pack=packs.get(device.pack_id) if device.pack_id else None,
            platform_label=labels.get((device.pack_id, device.platform_id)),
            static_group_keys=static_keys.get(device.id, frozenset()),
            now=now,
        )
        for device in device_list
    }


async def _load_intents_by_device_id(
    db: AsyncSession, device_ids: Sequence[UUID]
) -> dict[UUID, tuple[DeviceIntent, ...]]:
    rows = (await db.scalars(select(DeviceIntent).where(DeviceIntent.device_id.in_(device_ids)))).all()
    result: dict[UUID, list[DeviceIntent]] = {}
    for intent in rows:
        result.setdefault(intent.device_id, []).append(intent)
    return {device_id: tuple(items) for device_id, items in result.items()}


async def _load_live_session_device_ids(db: AsyncSession, device_ids: Sequence[UUID]) -> frozenset[UUID]:
    rows = await db.scalars(
        select(Session.device_id).where(Session.device_id.in_(device_ids), live_session_predicate())
    )
    # Session.device_id is nullable (device-less session terminalization); the IN
    # predicate already excludes NULL rows at the SQL level, this narrows the type.
    return frozenset(device_id for device_id in rows if device_id is not None)


def _require_complete_batch(device_ids: Sequence[UUID], *facts: tuple[str, Mapping[UUID, object]]) -> None:
    """Fail naming the loader and the devices, not with a bare ``KeyError``.

    The comprehension below indexes three batch maps directly, which is the
    right shape — every loader is contracted to answer for every id it was
    given. This turns a broken contract into a message an operator can act on
    instead of a UUID with no loader attached.
    """
    missing = {
        name: sorted(str(device_id) for device_id in device_ids if device_id not in mapping) for name, mapping in facts
    }
    incomplete = {name: ids for name, ids in missing.items() if ids}
    if incomplete:
        raise RuntimeError(f"device read projection is missing batch facts: {incomplete}")


def _build_device_read_projection(  # noqa: PLR0913 - one batch-loaded fact per parameter, no default to collapse
    device: Device,
    *,
    readiness: DeviceReadiness,
    operational_state: DeviceOperationalState,
    reservation: ReservationReadFacts | None,
    ladder: LadderState,
    intents: Sequence[DeviceIntent],
    live_session: bool,
    pack: PackView | None,
    platform_label: str | None,
    static_group_keys: frozenset[str],
    now: datetime,
) -> DeviceReadProjection:
    reservation_run_id, cooldown_active, cooldown_reason = reservation_decision_axes(
        run_id=reservation.run_id if reservation is not None else None,
        exclusion_kind=reservation.exclusion_kind if reservation is not None else None,
        exclusion_reason=reservation.exclusion_reason if reservation is not None else None,
        excluded_until=reservation.excluded_until if reservation is not None else None,
        now=now,
    )
    facts = DecisionFacts(
        in_maintenance=in_maintenance(device),
        device_checks_unhealthy=device.device_checks_healthy is False,
        in_service=WithdrawalFacts.from_device(device).in_service(),
        reservation_run_id=reservation_run_id,
        cooldown_active=cooldown_active,
        cooldown_reason=cooldown_reason,
        remediation_directive=ladder.node_directive,
    )
    commands = [command for intent in intents if (command := parse_command(intent, now)) is not None]
    return DeviceReadProjection(
        readiness=readiness,
        blocked_reason=pack_platform_resolver.evaluate_runnable(pack, platform_id=device.platform_id),
        operational_state=operational_state,
        reservation=reservation,
        ladder=ladder,
        recovery=recovery_availability_from_facts(
            commands=commands,
            facts=facts,
            ladder=ladder,
            live_session=live_session,
            ready=readiness.readiness_state == "verified",
            now=now,
        ),
        platform_label=platform_label,
        live_session=live_session,
        static_group_keys=static_group_keys,
        now=now,
    )
