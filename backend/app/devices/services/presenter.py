from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect, select

from app.appium_nodes.services.node_viability import device_node_accepting_new_sessions, device_node_is_viable
from app.core.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.devices.models import DeviceIntent
from app.devices.schemas.device import DeviceReservationRead
from app.devices.services import attention as device_attention
from app.devices.services import health as device_health
from app.devices.services import lifecycle_policy_summary
from app.devices.services import readiness as device_readiness
from app.devices.services.allocatability import unavailable_reason
from app.devices.services.intent_evaluator import (
    evaluate_grid_routing,
    evaluate_node_process,
    evaluate_recovery,
    evaluate_reservation,
)
from app.devices.services.intent_types import GRID_ROUTING, NODE_PROCESS, RECOVERY, RESERVATION
from app.devices.services.serialization_types import DeviceSerializationContext
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.packs.services import platform_resolver as pack_platform_resolver
from app.runs import service as run_service

assert_runnable = pack_platform_resolver.assert_runnable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.models import Device, DeviceReservation
    from app.runs.models import TestRun


def _cooldown_remaining_sec(reservation_entry: DeviceReservation | None) -> int | None:
    if reservation_entry is None or reservation_entry.excluded_until is None:
        return None
    remaining = int((reservation_entry.excluded_until - datetime.now(UTC)).total_seconds())
    return max(0, remaining)


class DevicePresenterService:
    def __init__(self, *, settings: SettingsReader) -> None:
        self._settings = settings

    async def build_serialization_contexts(
        self, db: AsyncSession, devices: list[Device]
    ) -> dict[uuid.UUID, DeviceSerializationContext]:
        """Batch-load everything :meth:`serialize_device` would otherwise query
        per device: readiness and blocked-reason both derive from a single load of
        the driver-pack catalog, collapsing the previous ~3-queries-per-device into one."""
        packs = await device_readiness.load_packs_by_ids(db, {device.pack_id for device in devices if device.pack_id})
        readiness_map = await device_readiness.assess_devices_async(db, devices, packs=packs)
        contexts: dict[uuid.UUID, DeviceSerializationContext] = {}
        for device in devices:
            pack = packs.get(device.pack_id) if device.pack_id else None
            contexts[device.id] = DeviceSerializationContext(
                readiness=readiness_map[device.id],
                blocked_reason=pack_platform_resolver.evaluate_runnable(pack, platform_id=device.platform_id),
            )
        return contexts

    async def serialize_device(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
        health_summary: dict[str, Any] | None = None,
        platform_label: str | None = None,
        precomputed: DeviceSerializationContext | None = None,
    ) -> dict[str, Any]:
        if reservation_context is None:
            reservation_context = await run_service.get_device_reservation_with_entry(db, device.id)
        reservation, reservation_entry = reservation_context
        is_reserved = reservation is not None
        # Gate-honest reservation: only a live, non-excluded reservation on a non-terminal
        # run actually blocks an arbitrary ticket (the same predicate the allocator uses).
        # Distinct from the broad ``is_reserved`` display flag above.
        reservation_blocks_allocation = run_service.reservation_gating_run_id(reservation, device.id) is not None
        # Load the node before the projection: the warm soft-gate reads
        # AppiumNode.accepting_new_sessions (the same flag _eligible_devices gates on).
        await _ensure_appium_node_loaded(db, device)
        node_accepting = device_node_accepting_new_sessions(device)
        node_viable = device_node_is_viable(device)
        allocatability_reason = unavailable_reason(
            device.operational_state,
            reserved=reservation_blocks_allocation,
            accepting_new_sessions=node_accepting,
            node_viable=node_viable,
        )
        readiness = (
            precomputed.readiness if precomputed is not None else await device_readiness.assess_device_async(db, device)
        )
        policy = await lifecycle_policy_summary.build_lifecycle_policy(
            db, device, reservation_context=reservation_context
        )
        lifecycle_summary = lifecycle_policy_summary.build_lifecycle_policy_summary(policy)
        if health_summary is None:
            health_summary = device_health.build_public_summary(device)
        hardware_status = hardware_telemetry.current_hardware_health_status(device)
        needs_attention = device_attention.compute_needs_attention(
            device.operational_state,
            readiness.readiness_state,
            hardware_health_status=hardware_status,
            review_required=bool(device.review_required),
        )

        emulator_state_value: str | None = None
        raw = device.emulator_state
        if isinstance(raw, str) and raw:
            emulator_state_value = raw

        blocked_reason: str | None
        if precomputed is not None:
            blocked_reason = precomputed.blocked_reason
        else:
            blocked_reason = None
            try:
                await assert_runnable(db, pack_id=device.pack_id, platform_id=device.platform_id)
            except (PackUnavailableError, PackDisabledError, PackDrainingError, PlatformRemovedError) as exc:
                blocked_reason = exc.code

        return {
            "id": device.id,
            "pack_id": device.pack_id,
            "platform_id": device.platform_id,
            "platform_label": platform_label,
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
            "operational_state": device.operational_state,
            "is_reserved": is_reserved,
            "allocatable": allocatability_reason is None,
            "unavailable_reason": allocatability_reason,
            "tags": device.tags,
            "device_type": device.device_type,
            "connection_type": device.connection_type,
            "ip_address": device.ip_address,
            "device_config": copy.deepcopy(device.device_config or {}),
            "battery_level_percent": device.battery_level_percent,
            "battery_temperature_c": device.battery_temperature_c,
            "charging_state": device.charging_state,
            "hardware_health_status": hardware_status,
            "hardware_telemetry_reported_at": device.hardware_telemetry_reported_at,
            "hardware_telemetry_state": hardware_telemetry.hardware_telemetry_state_for_device(
                device, settings=self._settings
            ),
            "readiness_state": readiness.readiness_state,
            "missing_setup_fields": readiness.missing_setup_fields,
            "verified_at": device.verified_at,
            "reservation": build_reservation_read(reservation, reservation_entry),
            "lifecycle_policy_summary": lifecycle_summary,
            "needs_attention": needs_attention,
            "health_summary": health_summary,
            "emulator_state": emulator_state_value,
            "blocked_reason": blocked_reason,
            "review_required": device.review_required,
            "review_reason": device.review_reason,
            "review_set_at": device.review_set_at,
            "created_at": device.created_at,
            "updated_at": device.updated_at,
        }

    async def serialize_device_detail(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
        health_summary: dict[str, Any] | None = None,
        platform_label: str | None = None,
    ) -> dict[str, Any]:
        payload = await self.serialize_device(
            db,
            device,
            reservation_context=reservation_context,
            health_summary=health_summary,
            platform_label=platform_label,
        )
        payload["appium_node"] = _serialize_appium_node_for_detail(device)
        payload["orchestration"] = await _serialize_orchestration(db, device)
        return payload


def build_reservation_read(
    reservation: TestRun | None,
    reservation_entry: DeviceReservation | None = None,
) -> DeviceReservationRead | None:
    if reservation is None:
        return None
    return DeviceReservationRead(
        run_id=reservation.id,
        run_name=reservation.name,
        run_state=reservation.state.value,
        excluded=run_service.reservation_entry_is_excluded(reservation_entry),
        exclusion_reason=reservation_entry.exclusion_reason if reservation_entry else None,
        excluded_until=reservation_entry.excluded_until if reservation_entry else None,
        cooldown_remaining_sec=_cooldown_remaining_sec(reservation_entry),
        cooldown_count=reservation_entry.cooldown_count if reservation_entry else 0,
        cooldown_escalated=bool(
            reservation_entry
            and reservation_entry.exclusion_reason
            and reservation_entry.exclusion_reason.startswith("Exceeded cooldown threshold ")
        ),
    )


async def _ensure_appium_node_loaded(db: AsyncSession, device: Device) -> None:
    if "appium_node" in inspect(device).unloaded:
        await db.refresh(device, attribute_names=["appium_node"])


def _serialize_appium_node_for_detail(device: Device) -> dict[str, Any] | None:
    node = device.appium_node
    if node is None:
        return None
    return {
        "id": node.id,
        "port": node.port,
        "pid": node.pid,
        "container_id": node.container_id,
        "active_connection_target": node.active_connection_target,
        "started_at": node.started_at,
        "desired_state": node.desired_state,
        "desired_port": node.desired_port,
        "transition_token": node.transition_token,
        "transition_deadline": node.transition_deadline,
        "last_observed_at": node.last_observed_at,
        "health_running": node.health_running,
        "health_state": node.health_state,
        "lifecycle_policy_state": copy.deepcopy(device.lifecycle_policy_state or {}),
    }


def _dataclass_to_dict(value: object) -> dict[str, Any]:
    return copy.deepcopy(getattr(value, "__dict__", {}))


async def _serialize_orchestration(db: AsyncSession, device: Device) -> dict[str, Any]:
    now = datetime.now(UTC)
    intents = (
        (
            await db.execute(
                select(DeviceIntent)
                .where(DeviceIntent.device_id == device.id)
                .order_by(DeviceIntent.axis, DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    node_intents = [intent for intent in intents if intent.axis == NODE_PROCESS]
    grid_intents = [intent for intent in intents if intent.axis == GRID_ROUTING]
    reservation_intents = [intent for intent in intents if intent.axis == RESERVATION]
    recovery_intents = [intent for intent in intents if intent.axis == RECOVERY]
    return {
        "intents": [
            {
                "source": intent.source,
                "axis": intent.axis,
                "run_id": intent.run_id,
                "payload": copy.deepcopy(intent.payload),
                "expires_at": intent.expires_at,
            }
            for intent in intents
        ],
        "derived": {
            "node_process": _dataclass_to_dict(evaluate_node_process(node_intents, now)),
            "grid_routing": _dataclass_to_dict(evaluate_grid_routing(grid_intents, now)),
            "reservation": _dataclass_to_dict(evaluate_reservation(reservation_intents, now)),
            "recovery": _dataclass_to_dict(evaluate_recovery(recovery_intents, now)),
        },
    }
