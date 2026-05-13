"""Allocated-device hydration helpers for GridFleet testkit consumers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from appium.webdriver.webdriver import WebDriver

    from .client import GridFleetClient
    from .types import JsonObject


@dataclass(frozen=True)
class UnavailableInclude:
    """One include key the backend could not satisfy on this allocation."""

    include: str
    reason: str


@dataclass(frozen=True)
class AllocatedDevice:
    """Combined view of an allocated device, ready for driver creation."""

    run_id: str
    device_id: str
    identity_value: str
    name: str
    pack_id: str
    platform_id: str
    platform_label: str | None
    os_version: str | None
    connection_target: str | None
    host_ip: str | None
    device_type: str
    connection_type: str
    manufacturer: str | None
    model: str | None
    config: JsonObject | None
    live_capabilities: JsonObject | None
    test_data: JsonObject | None = None
    unavailable_includes: tuple[UnavailableInclude, ...] = ()
    tags: dict[str, str] | None = None

    @property
    def is_real_device(self) -> bool:
        return self.device_type == "real_device"

    @property
    def is_simulator(self) -> bool:
        return self.device_type in {"simulator", "emulator"}

    @property
    def udid(self) -> str | None:
        if self.connection_target:
            return self.connection_target
        value = (self.live_capabilities or {}).get("appium:udid")
        return value if isinstance(value, str) and value else None

    @property
    def device_ip(self) -> str | None:
        """Best-effort address, preferring host IP before live device/config IP fields."""
        if self.host_ip:
            return self.host_ip
        live_value = (self.live_capabilities or {}).get("appium:deviceIP")
        if isinstance(live_value, str) and live_value:
            return live_value
        config_value = (self.config or {}).get("ip")
        return config_value if isinstance(config_value, str) and config_value else None

    @property
    def platform_name(self) -> str:
        return self.platform_label or self.platform_id


def _string_value(payload: JsonObject, key: str, *, default: str | None = None) -> str:
    value = payload.get(key, default)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"Allocated device payload is missing {key}")


def _optional_string_value(payload: JsonObject, key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _needs_device_detail(payload: JsonObject) -> bool:
    return any(payload.get(key) is None for key in ("name", "device_type", "connection_type", "manufacturer", "model"))


def _merge_device_detail(payload: JsonObject, detail: JsonObject) -> JsonObject:
    merged = dict(payload)
    for key in ("name", "device_type", "connection_type", "manufacturer", "model"):
        if merged.get(key) is None and detail.get(key) is not None:
            merged[key] = detail[key]
    if merged.get("host_ip") is None and detail.get("ip_address") is not None:
        merged["host_ip"] = detail["ip_address"]
    return merged


def _parse_unavailable_includes(payload: JsonObject) -> tuple[UnavailableInclude, ...]:
    raw = payload.get("unavailable_includes")
    if not isinstance(raw, list):
        return ()
    parsed: list[UnavailableInclude] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        include = entry.get("include")
        reason = entry.get("reason")
        if isinstance(include, str) and include and isinstance(reason, str) and reason:
            parsed.append(UnavailableInclude(include=include, reason=reason))
    return tuple(parsed)


def hydrate_allocated_device(
    device_handle: JsonObject,
    *,
    run_id: str,
    client: GridFleetClient,
    fetch_config: bool = True,
    fetch_capabilities: bool = False,
    fetch_test_data: bool = False,
) -> AllocatedDevice:
    """Combine a device handle with optional static config and live capabilities."""
    payload = dict(device_handle)
    device_id = _string_value(payload, "device_id")
    if _needs_device_detail(payload):
        payload = _merge_device_detail(payload, client.get_device(device_id))

    unavailable_includes = _parse_unavailable_includes(payload)
    unavailable_set = {entry.include for entry in unavailable_includes}

    connection_target = _optional_string_value(payload, "connection_target")
    inline_config = payload.get("config")
    if isinstance(inline_config, dict):
        config: JsonObject | None = cast("JsonObject", inline_config)
    elif fetch_config and connection_target and "config" not in unavailable_set:
        config = client.get_device_config(connection_target)
    else:
        config = None
    inline_capabilities = payload.get("live_capabilities")
    if isinstance(inline_capabilities, dict):
        live_capabilities: JsonObject | None = cast("JsonObject", inline_capabilities)
    elif fetch_capabilities and "capabilities" not in unavailable_set:
        live_capabilities = client.get_device_capabilities(device_id)
    else:
        live_capabilities = None

    inline_test_data = payload.get("test_data")
    if isinstance(inline_test_data, dict):
        test_data: JsonObject | None = cast("JsonObject", inline_test_data)
    elif fetch_test_data and "test_data" not in unavailable_set:
        test_data = client.get_device_test_data(device_id)
    else:
        test_data = None
    inline_tags = payload.get("tags")
    if isinstance(inline_tags, dict):
        tags: dict[str, str] | None = inline_tags
    else:
        tags = None

    return AllocatedDevice(
        run_id=run_id,
        device_id=device_id,
        identity_value=_string_value(payload, "identity_value"),
        name=_string_value(payload, "name", default=device_id),
        pack_id=_string_value(payload, "pack_id"),
        platform_id=_string_value(payload, "platform_id"),
        platform_label=_optional_string_value(payload, "platform_label"),
        os_version=_optional_string_value(payload, "os_version"),
        connection_target=connection_target,
        host_ip=_optional_string_value(payload, "host_ip"),
        device_type=_string_value(payload, "device_type"),
        connection_type=_string_value(payload, "connection_type"),
        manufacturer=_optional_string_value(payload, "manufacturer"),
        model=_optional_string_value(payload, "model"),
        config=config,
        live_capabilities=live_capabilities,
        test_data=test_data,
        unavailable_includes=unavailable_includes,
        tags=tags,
    )


def hydrate_allocated_device_from_driver(
    allocated: AllocatedDevice,
    driver: WebDriver,
    *,
    client: GridFleetClient,
) -> AllocatedDevice:
    """Refresh live capabilities from a running Appium driver session."""
    capabilities = getattr(driver, "capabilities", None)
    if isinstance(capabilities, dict):
        live_capabilities = cast("JsonObject", dict(capabilities))
    else:
        live_capabilities = client.get_device_capabilities(allocated.device_id)
    return replace(allocated, live_capabilities=live_capabilities)
