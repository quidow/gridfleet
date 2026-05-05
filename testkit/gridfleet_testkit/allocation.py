"""Allocated-device hydration helpers for GridFleet testkit consumers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import GridFleetClient


@dataclass(frozen=True)
class UnavailableInclude:
    """One include key the backend could not satisfy on this allocation."""

    include: str
    reason: str


@dataclass(frozen=True)
class AllocatedDevice:
    """Combined view of a claimed device, ready for driver creation."""

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
    claimed_by: str
    claimed_at: str
    config: dict[str, Any] | None
    live_capabilities: dict[str, Any] | None
    unavailable_includes: tuple[UnavailableInclude, ...] = ()
    config_is_masked: bool = False

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


def _string_value(payload: dict[str, Any], key: str, *, default: str | None = None) -> str:
    value = payload.get(key, default)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"Allocated device payload is missing {key}")


def _optional_string_value(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _needs_device_detail(payload: dict[str, Any]) -> bool:
    return any(payload.get(key) is None for key in ("name", "device_type", "connection_type", "manufacturer", "model"))


def _merge_device_detail(payload: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    for key in ("name", "device_type", "connection_type", "manufacturer", "model"):
        if merged.get(key) is None and detail.get(key) is not None:
            merged[key] = detail[key]
    if merged.get("host_ip") is None and detail.get("ip_address") is not None:
        merged["host_ip"] = detail["ip_address"]
    return merged


def hydrate_allocated_device(
    claim_response: dict[str, Any],
    *,
    run_id: str,
    client: GridFleetClient,
    fetch_config: bool = True,
    fetch_capabilities: bool = False,
) -> AllocatedDevice:
    """Combine a claim response with optional static config and live capabilities."""
    payload = dict(claim_response)
    device_id = _string_value(payload, "device_id")
    if _needs_device_detail(payload):
        payload = _merge_device_detail(payload, client.get_device(device_id))

    connection_target = _optional_string_value(payload, "connection_target")
    inline_config = payload.get("config")
    if isinstance(inline_config, dict):
        config: dict[str, Any] | None = inline_config
        config_is_masked = True
    elif fetch_config and connection_target:
        config = client.get_device_config(connection_target)
        config_is_masked = False
    else:
        config = None
        config_is_masked = False
    inline_capabilities = payload.get("live_capabilities")
    if isinstance(inline_capabilities, dict):
        live_capabilities: dict[str, Any] | None = inline_capabilities
    elif fetch_capabilities:
        live_capabilities = client.get_device_capabilities(device_id)
    else:
        live_capabilities = None

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
        claimed_by=_string_value(payload, "claimed_by"),
        claimed_at=_string_value(payload, "claimed_at"),
        config=config,
        config_is_masked=config_is_masked,
        live_capabilities=live_capabilities,
    )


def hydrate_allocated_device_from_driver(
    allocated: AllocatedDevice,
    driver: Any,
    *,
    client: GridFleetClient,
) -> AllocatedDevice:
    """Refresh live capabilities from a running Appium driver session."""
    capabilities = getattr(driver, "capabilities", None)
    if isinstance(capabilities, dict):
        live_capabilities = dict(capabilities)
    else:
        live_capabilities = client.get_device_capabilities(allocated.device_id)
    return replace(allocated, live_capabilities=live_capabilities)
