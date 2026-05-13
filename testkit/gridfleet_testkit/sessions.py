"""Public session payload helpers for GridFleet integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, cast

if TYPE_CHECKING:
    from appium.options.common import AppiumOptions
    from appium.webdriver.webdriver import WebDriver

    from .client import GridFleetClient
    from .types import JsonObject

__all__ = ["build_error_session_payload", "resolve_device_handle_from_driver"]

_KNOWN_DEVICE_TYPES = {"real_device", "emulator", "simulator"}
_KNOWN_CONNECTION_TYPES = {"usb", "network", "virtual"}


class ErrorSessionPayload(TypedDict):
    session_id: str
    test_name: str
    status: str
    requested_pack_id: str | None
    requested_platform_id: str | None
    requested_device_type: str | None
    requested_connection_type: str | None
    requested_capabilities: JsonObject
    error_type: str
    error_message: str


def _raw_attempted_capabilities(options: AppiumOptions) -> JsonObject:
    """Return capabilities attempted during driver creation."""
    capabilities = getattr(options, "capabilities", {})
    raw_capabilities = cast("JsonObject", dict(capabilities)) if isinstance(capabilities, dict) else {}
    platform_name = getattr(options, "platform_name", None)
    if isinstance(platform_name, str) and platform_name:
        raw_capabilities.setdefault("platformName", platform_name)
    return raw_capabilities


def _infer_requested_platform_id(
    raw_capabilities: JsonObject,
    *,
    platform_id: str | None = None,
) -> str | None:
    """Infer the GridFleet platform id from explicit input or attempted capabilities."""
    if platform_id:
        return platform_id
    platform_hint = raw_capabilities.get("appium:platform")
    return platform_hint if isinstance(platform_hint, str) and platform_hint else None


def _read_enum_capability(raw_capabilities: JsonObject, *keys: str, allowed: set[str]) -> str | None:
    """Read the first recognized enum-like capability value."""
    for key in keys:
        value = raw_capabilities.get(key)
        if isinstance(value, str) and value in allowed:
            return value
    return None


def build_error_session_payload(
    *,
    session_id: str,
    test_name: str,
    options: AppiumOptions,
    exc: Exception,
    pack_id: str | None = None,
    platform_id: str | None = None,
) -> ErrorSessionPayload:
    """Build a /api/sessions payload describing a driver-creation failure."""
    raw_capabilities = _raw_attempted_capabilities(options)
    return {
        "session_id": session_id,
        "test_name": test_name,
        "status": "error",
        "requested_pack_id": pack_id,
        "requested_platform_id": _infer_requested_platform_id(raw_capabilities, platform_id=platform_id),
        "requested_device_type": _read_enum_capability(
            raw_capabilities,
            "appium:device_type",
            "device_type",
            allowed=_KNOWN_DEVICE_TYPES,
        ),
        "requested_connection_type": _read_enum_capability(
            raw_capabilities,
            "appium:connection_type",
            "connection_type",
            allowed=_KNOWN_CONNECTION_TYPES,
        ),
        "requested_capabilities": raw_capabilities,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }


def resolve_device_handle_from_driver(driver: WebDriver, *, client: GridFleetClient) -> JsonObject:
    """Resolve a canonical device handle from a running WebDriver session."""
    caps = getattr(driver, "capabilities", None) or {}
    if not isinstance(caps, dict):
        raise RuntimeError("driver capabilities missing appium:udid; cannot resolve device handle")
    target = caps.get("appium:udid") or caps.get("appium:deviceName")
    if not target:
        raise RuntimeError("driver capabilities missing appium:udid; cannot resolve device handle")
    return client.get_device_by_connection_target(str(target))
