from typing import Any

from app.appium_nodes.services.capability_keys import core_manager_owned_cap_keys, sanitize_appium_caps
from app.devices.models import Device
from app.events.catalog import EventSeverity
from app.settings import settings_service


def node_state_severity(old_state: str, new_state: str) -> EventSeverity:
    """Derive severity from node state direction.

    running→stopped is actionable (worth a warning); stopped/error→running is
    a recovery (success); all other transitions are routine (info).
    """
    if new_state == "stopped" and old_state == "running":
        return "warning"
    if new_state == "running" and old_state != "running":
        return "success"
    return "info"


def get_default_plugins() -> list[str]:
    configured = settings_service.get("appium.default_plugins")
    if not isinstance(configured, str):
        return []
    return [plugin.strip() for plugin in configured.split(",") if plugin.strip()]


def build_extra_caps(
    device: Device,
    *,
    session_caps: dict[str, Any] | None = None,
    manager_owned_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    if device.id:
        extra["appium:gridfleet:deviceId"] = str(device.id)
    if device.name:
        extra["appium:gridfleet:deviceName"] = device.name
    if device.ip_address:
        extra["appium:ip"] = device.ip_address

    extra["appium:platform"] = device.platform_id
    extra["appium:device_type"] = device.device_type.value
    if device.os_version and device.os_version != "unknown":
        extra["appium:os_version"] = device.os_version
    if device.manufacturer:
        extra["appium:manufacturer"] = device.manufacturer
    if device.model:
        extra["appium:model"] = device.model

    config = device.device_config or {}
    owned = manager_owned_keys if manager_owned_keys is not None else core_manager_owned_cap_keys()
    extra.update(sanitize_appium_caps(config.get("appium_caps"), manager_owned=owned))
    if session_caps:
        extra.update(session_caps)
    if device.tags:
        for key, value in device.tags.items():
            extra[f"appium:gridfleet:tag:{key}"] = value

    return extra


def build_appium_driver_caps(
    device: Device,
    *,
    session_caps: dict[str, Any] | None = None,
    manager_owned_keys: frozenset[str] | None = None,
) -> dict[str, Any]:
    config = device.device_config or {}
    owned = manager_owned_keys if manager_owned_keys is not None else core_manager_owned_cap_keys()
    caps = sanitize_appium_caps(config.get("appium_caps"), manager_owned=owned)
    if session_caps:
        caps.update(session_caps)
    return caps


def build_grid_stereotype_caps(
    device: Device,
    *,
    pack_stereotype: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the Selenium Grid slot stereotype for *device*.

    The stereotype is the per-slot routing surface used by the Selenium hub to
    match incoming session requests against nodes. Appium-driver-side caps
    (manufacturer, model, ip, sanitized device_config caps) deliberately stay
    out — they flow to the driver via ``extra_caps`` instead.
    """
    stereotype: dict[str, Any] = {}
    if pack_stereotype:
        stereotype.update(pack_stereotype)
    if device.id:
        stereotype["appium:gridfleet:deviceId"] = str(device.id)
    if device.tags:
        for key, value in device.tags.items():
            stereotype[f"appium:gridfleet:tag:{key}"] = value
    return stereotype
