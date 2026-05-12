from __future__ import annotations

import copy
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import inspect, select

from app.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.models.device_intent import DeviceIntent
from app.schemas.device import DeviceReservationRead
from app.services import (
    device_attention,
    device_health,
    device_readiness,
    hardware_telemetry,
    lifecycle_policy,
    run_service,
)
from app.services.intent_evaluator import (
    evaluate_grid_routing,
    evaluate_node_process,
    evaluate_recovery,
    evaluate_reservation,
)
from app.services.intent_types import GRID_ROUTING, NODE_PROCESS, RECOVERY, RESERVATION
from app.services.pack_platform_resolver import assert_runnable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device
    from app.models.device_reservation import DeviceReservation
    from app.models.test_run import TestRun


def _cooldown_remaining_sec(reservation_entry: DeviceReservation | None) -> int | None:
    if reservation_entry is None or reservation_entry.excluded_until is None:
        return None
    remaining = int((reservation_entry.excluded_until - datetime.now(UTC)).total_seconds())
    return max(0, remaining)


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
        "grid_url": node.grid_url,
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


async def serialize_device(
    db: AsyncSession,
    device: Device,
    reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
    health_summary: dict[str, Any] | None = None,
    platform_label: str | None = None,
) -> dict[str, Any]:
    if reservation_context is None:
        reservation_context = await run_service.get_device_reservation_with_entry(db, device.id)
    reservation, reservation_entry = reservation_context
    readiness = await device_readiness.assess_device_async(db, device)
    policy = await lifecycle_policy.build_lifecycle_policy(db, device, reservation_context=reservation_context)
    lifecycle_summary = lifecycle_policy.build_lifecycle_policy_summary(policy)
    await _ensure_appium_node_loaded(db, device)
    if health_summary is None:
        health_summary = device_health.build_public_summary(device)
    hardware_status = hardware_telemetry.current_hardware_health_status(device)
    needs_attention = device_attention.compute_needs_attention(
        lifecycle_summary["state"],
        readiness.readiness_state,
        health_healthy=health_summary.get("healthy") if health_summary else None,
        hardware_health_status=hardware_status,
    )

    emulator_state_value: str | None = None
    raw = device.emulator_state
    if isinstance(raw, str) and raw:
        emulator_state_value = raw

    blocked_reason: str | None = None
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
        "manufacturer": device.manufacturer,
        "model": device.model,
        "model_number": device.model_number,
        "software_versions": device.software_versions,
        "host_id": device.host_id,
        "operational_state": device.operational_state,
        "hold": device.hold,
        "tags": device.tags,
        "auto_manage": device.auto_manage,
        "device_type": device.device_type,
        "connection_type": device.connection_type,
        "ip_address": device.ip_address,
        "device_config": copy.deepcopy(device.device_config or {}),
        "battery_level_percent": device.battery_level_percent,
        "battery_temperature_c": device.battery_temperature_c,
        "charging_state": device.charging_state,
        "hardware_health_status": hardware_status,
        "hardware_telemetry_reported_at": device.hardware_telemetry_reported_at,
        "hardware_telemetry_state": hardware_telemetry.hardware_telemetry_state_for_device(device),
        "readiness_state": readiness.readiness_state,
        "missing_setup_fields": readiness.missing_setup_fields,
        "verified_at": device.verified_at,
        "reservation": build_reservation_read(reservation, reservation_entry),
        "lifecycle_policy_summary": lifecycle_summary,
        "needs_attention": needs_attention,
        "health_summary": health_summary,
        "emulator_state": emulator_state_value,
        "blocked_reason": blocked_reason,
        "created_at": device.created_at,
        "updated_at": device.updated_at,
    }


async def serialize_device_detail(
    db: AsyncSession,
    device: Device,
    reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
    health_summary: dict[str, Any] | None = None,
    platform_label: str | None = None,
) -> dict[str, Any]:
    payload = await serialize_device(
        db,
        device,
        reservation_context,
        health_summary=health_summary,
        platform_label=platform_label,
    )
    payload["appium_node"] = _serialize_appium_node_for_detail(device)
    payload["sessions"] = device.sessions
    payload["orchestration"] = await _serialize_orchestration(db, device)
    return payload
