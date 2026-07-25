from __future__ import annotations

import copy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect, select

from app.appium_nodes.services.node_viability import device_node_accepting_new_sessions, device_node_is_viable
from app.core.timeutil import now_utc
from app.devices.models import DeviceIntent, ExclusionKind
from app.devices.schemas.device import DeviceReservationRead
from app.devices.services import attention as device_attention
from app.devices.services import health as device_health
from app.devices.services.allocatability import unavailable_reason
from app.devices.services.decision import (
    decide_grid_routing,
    decide_node_process,
    decide_recovery,
    parse_command,
)
from app.devices.services.intent_reconciler import gather_decision_facts
from app.devices.services.lifecycle_policy_summary import (
    build_lifecycle_policy_from_facts,
    build_lifecycle_policy_summary,
)
from app.devices.services.read_projection import load_device_read_projections
from app.lifecycle.services import remediation_log

DEFAULT_RESTART_WINDOW_SEC = 120

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.services.serialization_types import DeviceReadProjection, ReservationReadFacts


class DevicePresenterService:
    def serialize_projected_device(self, device: Device, projection: DeviceReadProjection) -> dict[str, Any]:
        reservation = projection.reservation
        reservation_blocks_allocation = bool(reservation and reservation.blocks_allocation)
        allocatability_reason = unavailable_reason(
            projection.operational_state,
            reserved=reservation_blocks_allocation,
            accepting_new_sessions=device_node_accepting_new_sessions(device),
            node_viable=device_node_is_viable(
                device, now=projection.now, restart_window_sec=DEFAULT_RESTART_WINDOW_SEC
            ),
        )
        policy = build_lifecycle_policy_from_facts(
            device,
            ladder=projection.ladder,
            reservation=reservation,
            availability=projection.recovery,
            operational_state=projection.operational_state,
            now=projection.now,
        )
        health_summary = device_health.build_public_summary(device, policy_view=policy)
        needs_attention = device_attention.compute_needs_attention(
            projection.operational_state,
            projection.readiness.readiness_state,
            review_required=bool(device.review_required),
        )
        return {
            "id": device.id,
            "pack_id": device.pack_id,
            "platform_id": device.platform_id,
            "platform_label": projection.platform_label,
            "identity_scheme": device.identity_scheme,
            "identity_scope": device.identity_scope,
            "identity_value": device.identity_value,
            "connection_target": device.connection_target,
            "name": device.name,
            "os_version": device.os_version,
            "os_version_display": device.os_version_display,
            "manufacturer": device.manufacturer,
            "model": device.model,
            "model_number": device.model_number,
            "software_versions": device.software_versions,
            "host_id": device.host_id,
            "operational_state": projection.operational_state,
            "is_reserved": reservation is not None,
            "allocatable": allocatability_reason is None,
            "unavailable_reason": allocatability_reason,
            "device_type": device.device_type,
            "connection_type": device.connection_type,
            "ip_address": device.ip_address,
            "device_config": copy.deepcopy(device.device_config or {}),
            "readiness_state": projection.readiness.readiness_state,
            "missing_setup_fields": projection.readiness.missing_setup_fields,
            "verified_at": device.verified_at,
            "reservation": build_reservation_read_from_facts(reservation, now=projection.now),
            "lifecycle_policy_summary": build_lifecycle_policy_summary(policy),
            "needs_attention": needs_attention,
            "health_summary": health_summary,
            "blocked_reason": projection.blocked_reason,
            "review_required": device.review_required,
            "review_reason": device.review_reason,
            "review_set_at": device.review_set_at,
            "created_at": device.created_at,
            "updated_at": device.updated_at,
        }

    async def serialize_device(
        self,
        db: AsyncSession,
        device: Device,
        *,
        platform_label: str | None = None,
    ) -> dict[str, Any]:
        # ``load_device_read_projections`` relies on the declared read graph and
        # does not load ``appium_node`` itself; single-device callers (CRUD, control
        # routes, discovery) may hand us a row with it unloaded, so ensure it before
        # the projection reads it via the node-viability / public-summary helpers.
        await _ensure_appium_node_loaded(db, device)
        projection = (await load_device_read_projections(db, [device], now=now_utc()))[device.id]
        # A caller supplying its own already-resolved label wins; otherwise the
        # projection's batch-resolved label carries.
        if platform_label is not None:
            projection = replace(projection, platform_label=platform_label)
        return self.serialize_projected_device(device, projection)

    async def serialize_device_detail(
        self,
        db: AsyncSession,
        device: Device,
        *,
        platform_label: str | None = None,
        include_orchestration: bool = False,
    ) -> dict[str, Any]:
        ladder = await remediation_log.load_ladder(db, device.id)
        policy_view = remediation_log.build_policy_view(ladder, device.lifecycle_policy_state)
        payload = await self.serialize_device(db, device, platform_label=platform_label)
        payload["appium_node"] = _serialize_appium_node_for_detail(device, policy_view=policy_view)
        if include_orchestration:
            payload["orchestration"] = await _serialize_orchestration(db, device)
        return payload


def _reservation_read_dto(
    *,
    run_id: uuid.UUID,
    run_name: str,
    run_state: str,
    excluded: bool,
    exclusion_reason: str | None,
    excluded_until: datetime | None,
    cooldown_count: int,
    cooldown_remaining_sec: int | None,
) -> DeviceReservationRead:
    return DeviceReservationRead(
        run_id=run_id,
        run_name=run_name,
        run_state=run_state,
        excluded=excluded,
        exclusion_reason=exclusion_reason,
        excluded_until=excluded_until,
        cooldown_remaining_sec=cooldown_remaining_sec,
        cooldown_count=cooldown_count,
        cooldown_escalated=bool(exclusion_reason and exclusion_reason.startswith("Exceeded cooldown threshold ")),
    )


def build_reservation_read_from_facts(
    reservation: ReservationReadFacts | None,
    *,
    now: datetime,
) -> DeviceReservationRead | None:
    if reservation is None:
        return None
    cooldown_remaining_sec: int | None = None
    if reservation.exclusion_kind == ExclusionKind.cooldown and reservation.excluded_until is not None:
        cooldown_remaining_sec = max(0, int((reservation.excluded_until - now).total_seconds()))
    return _reservation_read_dto(
        run_id=reservation.run_id,
        run_name=reservation.run_name,
        run_state=reservation.run_state,
        excluded=reservation.excluded,
        exclusion_reason=reservation.exclusion_reason,
        excluded_until=reservation.excluded_until,
        cooldown_count=reservation.cooldown_count,
        cooldown_remaining_sec=cooldown_remaining_sec,
    )


async def _ensure_appium_node_loaded(db: AsyncSession, device: Device) -> None:
    if "appium_node" in inspect(device).unloaded:
        await db.refresh(device, attribute_names=["appium_node"])


def _serialize_appium_node_for_detail(device: Device, *, policy_view: dict[str, Any]) -> dict[str, Any] | None:
    node = device.appium_node
    if node is None:
        return None
    return {
        "id": node.id,
        "port": node.port,
        "pid": node.pid,
        "active_connection_target": node.active_connection_target,
        "started_at": node.started_at,
        "desired_state": node.desired_state,
        "desired_port": node.desired_port,
        "restart_requested_at": node.restart_requested_at,
        "last_observed_at": node.last_observed_at,
        "health_running": node.health_running,
        "health_state": node.health_state,
        "lifecycle_policy_state": copy.deepcopy(policy_view),
        "review_required": device.review_required,
    }


def _dataclass_to_dict(value: object) -> dict[str, Any]:
    return copy.deepcopy(getattr(value, "__dict__", {}))


async def _serialize_orchestration(db: AsyncSession, device: Device) -> dict[str, Any]:
    now = now_utc()
    intents = (
        (
            await db.execute(
                select(DeviceIntent)
                .where(DeviceIntent.device_id == device.id)
                .order_by(DeviceIntent.kind, DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    commands = [c for c in (parse_command(row, now) for row in intents) if c is not None]
    facts = await gather_decision_facts(db, device, now)
    return {
        "intents": [
            {
                "source": intent.source,
                "kind": intent.kind,
                "run_id": intent.run_id,
                "payload": copy.deepcopy(intent.payload),
                "expires_at": intent.expires_at,
            }
            for intent in intents
        ],
        "derived": {
            "node_process": _dataclass_to_dict(decide_node_process(commands, facts)),
            "grid_routing": _dataclass_to_dict(decide_grid_routing(facts)),
            "recovery": _dataclass_to_dict(decide_recovery(commands, facts)),
        },
    }
