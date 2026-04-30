from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.errors import PackDisabledError, PackDrainingError, PackUnavailableError, PlatformRemovedError
from app.schemas.device import DeviceReservationRead
from app.services import (
    device_attention,
    device_config_masking,
    device_health_summary,
    device_readiness,
    hardware_telemetry,
    lifecycle_policy,
    run_service,
)
from app.services.pack_platform_resolver import assert_runnable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device
    from app.models.device_reservation import DeviceReservation
    from app.models.test_run import TestRun


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
    )


async def serialize_device(
    db: AsyncSession,
    device: Device,
    reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
    health_summary: dict[str, Any] | None = None,
    platform_label: str | None = None,
    sensitive_key_map: Mapping[tuple[str, str], set[str]] | None = None,
) -> dict[str, Any]:
    if reservation_context is None:
        reservation_context = await run_service.get_device_reservation_with_entry(db, device.id)
    reservation, reservation_entry = reservation_context
    readiness = await device_readiness.assess_device_async(db, device)
    policy = await lifecycle_policy.build_lifecycle_policy(db, device, reservation_context=reservation_context)
    lifecycle_summary = lifecycle_policy.build_lifecycle_policy_summary(policy)
    if health_summary is None:
        snapshot = await device_health_summary.get_health_snapshot(db, str(device.id))
        health_summary = device_health_summary.build_public_health_summary(snapshot)
    else:
        snapshot = None
    hardware_status = hardware_telemetry.current_hardware_health_status(device)
    needs_attention = device_attention.compute_needs_attention(
        lifecycle_summary["state"],
        readiness.readiness_state,
        health_healthy=health_summary.get("healthy") if health_summary else None,
        hardware_health_status=hardware_status,
    )

    emulator_state_value: str | None = None
    if snapshot is not None:
        raw = snapshot.get("emulator_state")
        if isinstance(raw, str) and raw:
            emulator_state_value = raw
    elif health_summary is not None:
        # If we already had a snapshot from a pre-built health_summary, try fetching it
        fresh_snapshot = await device_health_summary.get_health_snapshot(db, str(device.id))
        if fresh_snapshot is not None:
            raw = fresh_snapshot.get("emulator_state")
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
        "availability_status": device.availability_status,
        "tags": device.tags,
        "auto_manage": device.auto_manage,
        "device_type": device.device_type,
        "connection_type": device.connection_type,
        "ip_address": device.ip_address,
        "device_config": await device_config_masking.mask_device_config(
            db,
            device,
            device.device_config,
            sensitive_key_map=sensitive_key_map,
        ),
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
    sensitive_key_map: Mapping[tuple[str, str], set[str]] | None = None,
) -> dict[str, Any]:
    payload = await serialize_device(
        db,
        device,
        reservation_context,
        health_summary=health_summary,
        platform_label=platform_label,
        sensitive_key_map=sensitive_key_map,
    )
    payload["appium_node"] = device.appium_node
    payload["sessions"] = device.sessions
    return payload
