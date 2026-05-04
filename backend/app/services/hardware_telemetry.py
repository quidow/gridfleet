from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.errors import AgentCallError
from app.models.device import (
    Device,
    DeviceAvailabilityStatus,
    DeviceType,
    HardwareChargingState,
    HardwareHealthStatus,
    HardwareTelemetrySupportStatus,
)
from app.models.device_event import DeviceEventType
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop, parse_timestamp
from app.schemas.device import HardwareTelemetryState
from app.services import control_plane_state_store
from app.services.agent_operations import pack_device_telemetry as fetch_pack_device_telemetry
from app.services.device_event_service import record_event
from app.services.event_bus import queue_event_for_session
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)

LOOP_NAME = "hardware_telemetry"
HARDWARE_TELEMETRY_STATE_NAMESPACE = "hardware_telemetry.state"
WARNING_OR_WORSE = {HardwareHealthStatus.warning, HardwareHealthStatus.critical}


def _now() -> datetime:
    return datetime.now(UTC)


def _health_rank(status: HardwareHealthStatus) -> int:
    ranks = {
        HardwareHealthStatus.unknown: 0,
        HardwareHealthStatus.healthy: 1,
        HardwareHealthStatus.warning: 2,
        HardwareHealthStatus.critical: 3,
    }
    return ranks[status]


def _coerce_charging_state(value: object) -> HardwareChargingState | None:
    if not isinstance(value, str):
        return None
    try:
        return HardwareChargingState(value)
    except ValueError:
        return None


def _coerce_support_status(value: object) -> HardwareTelemetrySupportStatus:
    if not isinstance(value, str):
        return HardwareTelemetrySupportStatus.unknown
    try:
        return HardwareTelemetrySupportStatus(value)
    except ValueError:
        return HardwareTelemetrySupportStatus.unknown


def _coerce_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _coerce_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def current_hardware_health_status(device: Device) -> HardwareHealthStatus:
    if isinstance(device.hardware_health_status, HardwareHealthStatus):
        return device.hardware_health_status
    return HardwareHealthStatus.unknown


def current_hardware_support_status(device: Device) -> HardwareTelemetrySupportStatus:
    if isinstance(device.hardware_telemetry_support_status, HardwareTelemetrySupportStatus):
        return device.hardware_telemetry_support_status
    return HardwareTelemetrySupportStatus.unknown


def hardware_telemetry_state_for_device(
    device: Device,
    *,
    now: datetime | None = None,
    stale_timeout_sec: int | None = None,
) -> HardwareTelemetryState:
    if device.device_type != DeviceType.real_device:
        return HardwareTelemetryState.unsupported

    support_status = current_hardware_support_status(device)
    if support_status == HardwareTelemetrySupportStatus.unsupported:
        return HardwareTelemetryState.unsupported

    reported_at = device.hardware_telemetry_reported_at
    if support_status != HardwareTelemetrySupportStatus.supported or reported_at is None:
        return HardwareTelemetryState.unknown

    timeout_seconds = stale_timeout_sec or int(settings_service.get("general.hardware_telemetry_stale_timeout_sec"))
    if (now or _now()) - reported_at > timedelta(seconds=timeout_seconds):
        return HardwareTelemetryState.stale
    return HardwareTelemetryState.fresh


def derive_candidate_hardware_health_status(device: Device) -> HardwareHealthStatus:
    support_status = current_hardware_support_status(device)
    if support_status != HardwareTelemetrySupportStatus.supported:
        return HardwareHealthStatus.unknown

    temperature = device.battery_temperature_c
    critical_threshold = float(settings_service.get("general.hardware_temperature_critical_c"))
    warning_threshold = float(settings_service.get("general.hardware_temperature_warning_c"))
    if temperature is not None and temperature >= critical_threshold:
        return HardwareHealthStatus.critical
    if temperature is not None and temperature >= warning_threshold:
        return HardwareHealthStatus.warning
    if (
        device.battery_level_percent is not None
        or (device.charging_state is not None and device.charging_state != HardwareChargingState.unknown)
        or device.battery_temperature_c is not None
    ):
        return HardwareHealthStatus.healthy
    return HardwareHealthStatus.unknown


async def _resolve_effective_hardware_health_status(
    db: AsyncSession,
    device: Device,
    candidate_status: HardwareHealthStatus,
) -> HardwareHealthStatus:
    previous_status = current_hardware_health_status(device)
    if candidate_status == previous_status:
        await control_plane_state_store.delete_value(db, HARDWARE_TELEMETRY_STATE_NAMESPACE, str(device.id))
        return previous_status

    if candidate_status not in WARNING_OR_WORSE or _health_rank(candidate_status) <= _health_rank(previous_status):
        await control_plane_state_store.delete_value(db, HARDWARE_TELEMETRY_STATE_NAMESPACE, str(device.id))
        return candidate_status

    required_samples = max(1, int(settings_service.get("general.hardware_telemetry_consecutive_samples")))
    state = await control_plane_state_store.get_value(db, HARDWARE_TELEMETRY_STATE_NAMESPACE, str(device.id))
    current_count = 0
    current_candidate = None
    if isinstance(state, dict):
        current_count = _coerce_int(state.get("consecutive_samples")) or 0
        candidate_value = state.get("candidate_status")
        if isinstance(candidate_value, str):
            current_candidate = candidate_value

    next_count = current_count + 1 if current_candidate == candidate_status.value else 1
    if next_count >= required_samples:
        await control_plane_state_store.delete_value(db, HARDWARE_TELEMETRY_STATE_NAMESPACE, str(device.id))
        return candidate_status

    await control_plane_state_store.set_value(
        db,
        HARDWARE_TELEMETRY_STATE_NAMESPACE,
        str(device.id),
        {
            "candidate_status": candidate_status.value,
            "consecutive_samples": next_count,
        },
    )
    return previous_status


def _event_payload(
    device: Device,
    *,
    old_status: HardwareHealthStatus,
    new_status: HardwareHealthStatus,
) -> dict[str, Any]:
    return {
        "device_id": str(device.id),
        "device_name": device.name,
        "old_status": old_status.value,
        "new_status": new_status.value,
        "battery_level_percent": device.battery_level_percent,
        "battery_temperature_c": device.battery_temperature_c,
        "charging_state": device.charging_state.value if device.charging_state is not None else None,
        "reported_at": (
            device.hardware_telemetry_reported_at.isoformat()
            if device.hardware_telemetry_reported_at is not None
            else None
        ),
    }


async def apply_telemetry_sample(
    db: AsyncSession,
    device: Device,
    sample: dict[str, Any],
) -> HardwareHealthStatus:
    device.battery_level_percent = _coerce_int(sample.get("battery_level_percent"))
    device.battery_temperature_c = _coerce_float(sample.get("battery_temperature_c"))
    device.charging_state = _coerce_charging_state(sample.get("charging_state"))
    device.hardware_telemetry_support_status = _coerce_support_status(sample.get("support_status"))

    reported_at = parse_timestamp(sample.get("reported_at"))
    device.hardware_telemetry_reported_at = reported_at or _now()

    previous_status = current_hardware_health_status(device)
    candidate_status = derive_candidate_hardware_health_status(device)
    next_status = await _resolve_effective_hardware_health_status(db, device, candidate_status)
    device.hardware_health_status = next_status
    await db.flush()

    if next_status != previous_status and next_status in WARNING_OR_WORSE:
        payload = _event_payload(device, old_status=previous_status, new_status=next_status)
        await record_event(
            db,
            device.id,
            DeviceEventType.hardware_health_changed,
            payload,
        )
        queue_event_for_session(db, "device.hardware_health_changed", payload)

    return next_status


async def _get_device_telemetry(device: Device) -> dict[str, Any] | None:
    host = device.host
    if host is None or device.connection_target is None:
        return None

    try:
        return await fetch_pack_device_telemetry(
            host.ip,
            host.agent_port,
            device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value,
            connection_type=device.connection_type.value if device.connection_type is not None else None,
            ip_address=device.ip_address,
            http_client_factory=httpx.AsyncClient,
        )
    except AgentCallError:
        return None


async def poll_hardware_telemetry_once(db: AsyncSession) -> None:
    stmt = (
        select(Device)
        .join(Host)
        .where(
            Host.status == HostStatus.online,
            Device.device_type == DeviceType.real_device,
            Device.availability_status != DeviceAvailabilityStatus.offline,
        )
        .options(selectinload(Device.host))
    )
    result = await db.execute(stmt)
    devices = result.scalars().all()

    for device in devices:
        try:
            telemetry = await _get_device_telemetry(device)
            if telemetry is None:
                continue
            await apply_telemetry_sample(db, device, telemetry)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("Failed to poll hardware telemetry for device %s", device.identity_value)


async def hardware_telemetry_loop() -> None:
    while True:
        interval = float(settings_service.get("general.hardware_telemetry_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await poll_hardware_telemetry_once(db)
        except Exception:
            logger.exception("Hardware telemetry loop failed")
        await asyncio.sleep(interval)
