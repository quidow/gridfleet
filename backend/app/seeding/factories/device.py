"""Device factory — builds ORM Device instances across supported demo platforms."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from app.models.device import (
    ConnectionType,
    Device,
    DeviceHold,
    DeviceOperationalState,
    DeviceType,
    HardwareChargingState,
    HardwareHealthStatus,
    HardwareTelemetrySupportStatus,
)

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from app.seeding.context import SeedContext


BATTERY_PLATFORM_IDS = {"android_mobile", "ios"}
SeedDeviceStatus = DeviceOperationalState | DeviceHold

# Mapping from platform_id to (pack_id, default identity_scheme, default identity_scope)
_PACK_IDENTITY_BY_PLATFORM_ID: dict[str, tuple[str, str, str]] = {
    "android_mobile": ("appium-uiautomator2", "android_serial", "host"),
    "android_tv": ("appium-uiautomator2", "android_serial", "host"),
    "firetv_real": ("appium-uiautomator2", "android_serial", "host"),
    "ios": ("appium-xcuitest", "apple_udid", "global"),
    "tvos": ("appium-xcuitest", "apple_udid", "global"),
    "roku_network": ("appium-roku-dlenroc", "roku_serial", "global"),
}


def _pack_identity_for(platform_id: str, device_type: DeviceType) -> tuple[str, str, str]:
    pack_id, identity_scheme, identity_scope = _PACK_IDENTITY_BY_PLATFORM_ID.get(
        platform_id,
        ("appium-uiautomator2", "android_serial", "host"),
    )
    if platform_id in {"ios", "tvos"} and device_type is DeviceType.simulator:
        return pack_id, "simulator_udid", "host"
    return pack_id, identity_scheme, identity_scope


def _has_battery(platform_id: str) -> bool:
    return platform_id in BATTERY_PLATFORM_IDS


def make_device(
    ctx: SeedContext,
    *,
    host_id: uuid.UUID,
    platform_id: str,
    device_type: DeviceType,
    connection_type: ConnectionType,
    identity_value: str,
    name: str,
    model: str,
    manufacturer: str,
    os_version: str,
    status: SeedDeviceStatus = DeviceOperationalState.available,
    verified: bool = True,
    extra_tags: dict[str, str] | None = None,
    connection_target: str | None = None,
    ip_address: str | None = None,
    device_config: dict[str, object] | None = None,
    hardware_health_status: HardwareHealthStatus | None = None,
    hardware_telemetry_support_status: HardwareTelemetrySupportStatus | None = None,
    hardware_telemetry_reported_at: datetime | None = None,
    battery_level_percent: int | None = None,
    battery_temperature_c: float | None = None,
    charging_state: HardwareChargingState | None = None,
    pack_id: str | None = None,
    identity_scheme: str | None = None,
    identity_scope: str | None = None,
) -> Device:
    """Build an unflushed Device with platform-appropriate identity + tags."""
    tags: dict[str, str] = dict(extra_tags or {})

    # Derive pack_id, identity_scheme, identity_scope from platform_id if not provided
    default_pack_id, default_scheme, default_scope = _pack_identity_for(platform_id, device_type)
    resolved_pack_id = pack_id or default_pack_id
    resolved_scheme = identity_scheme or default_scheme
    resolved_scope = identity_scope or default_scope

    # Hardware health defaults: real devices report fresh "healthy" telemetry,
    # emulators/simulators don't support hardware telemetry, offline devices
    # have no recent report. Callers can override any field explicitly.
    is_real = device_type is DeviceType.real_device
    operational_state = status if isinstance(status, DeviceOperationalState) else DeviceOperationalState.available
    hold = status if isinstance(status, DeviceHold) else None
    is_online = operational_state in {
        DeviceOperationalState.available,
        DeviceOperationalState.busy,
    }

    if hardware_telemetry_support_status is None:
        hardware_telemetry_support_status = (
            HardwareTelemetrySupportStatus.supported if is_real else HardwareTelemetrySupportStatus.unsupported
        )
    telemetry_supported = hardware_telemetry_support_status is HardwareTelemetrySupportStatus.supported

    if hardware_health_status is None:
        hardware_health_status = (
            HardwareHealthStatus.healthy if telemetry_supported and is_online else HardwareHealthStatus.unknown
        )

    if hardware_telemetry_reported_at is None and telemetry_supported and is_online:
        hardware_telemetry_reported_at = ctx.now - timedelta(seconds=ctx.rng.randint(5, 300))

    if battery_level_percent is None and _has_battery(platform_id) and is_real and is_online:
        battery_level_percent = ctx.rng.randint(30, 99)
    if battery_temperature_c is None and _has_battery(platform_id) and is_real and is_online:
        battery_temperature_c = round(ctx.rng.uniform(24.0, 38.0), 1)
    if charging_state is None and _has_battery(platform_id) and is_real and is_online:
        charging_state = ctx.rng.choice(
            [
                HardwareChargingState.charging,
                HardwareChargingState.discharging,
                HardwareChargingState.full,
            ]
        )
    device = Device(
        pack_id=resolved_pack_id,
        platform_id=platform_id,
        identity_scheme=resolved_scheme,
        identity_scope=resolved_scope,
        identity_value=identity_value,
        connection_target=connection_target or identity_value,
        name=name,
        os_version=os_version,
        host_id=host_id,
        operational_state=operational_state,
        hold=hold,
        tags=tags,
        manufacturer=manufacturer,
        model=model,
        device_type=device_type,
        connection_type=connection_type,
        ip_address=ip_address,
        device_config=device_config or {},
        hardware_health_status=hardware_health_status,
        hardware_telemetry_support_status=hardware_telemetry_support_status,
        hardware_telemetry_reported_at=hardware_telemetry_reported_at,
        battery_level_percent=battery_level_percent,
        battery_temperature_c=battery_temperature_c,
        charging_state=charging_state,
    )
    if verified:
        device.verified_at = ctx.now
    return device
