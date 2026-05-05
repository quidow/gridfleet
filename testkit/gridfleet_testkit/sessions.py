"""Public session payload helpers for GridFleet integrations."""

from __future__ import annotations

from typing import Any

KNOWN_DEVICE_TYPES = {"real_device", "emulator", "simulator"}
KNOWN_CONNECTION_TYPES = {"usb", "network", "virtual"}


def raw_attempted_capabilities(options: Any) -> dict[str, Any]:
    """Return capabilities attempted during driver creation."""
    capabilities = getattr(options, "capabilities", {})
    raw_capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
    platform_name = getattr(options, "platform_name", None)
    if isinstance(platform_name, str) and platform_name:
        raw_capabilities.setdefault("platformName", platform_name)
    return raw_capabilities


def infer_requested_platform_id(
    raw_capabilities: dict[str, Any],
    *,
    platform_id: str | None = None,
) -> str | None:
    """Infer the GridFleet platform id from explicit input or attempted capabilities."""
    if platform_id:
        return platform_id
    platform_hint = raw_capabilities.get("appium:platform")
    return platform_hint if isinstance(platform_hint, str) and platform_hint else None


def read_enum_capability(raw_capabilities: dict[str, Any], *keys: str, allowed: set[str]) -> str | None:
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
    options: Any,
    exc: Exception,
    pack_id: str | None = None,
    platform_id: str | None = None,
) -> dict[str, Any]:
    """Build a /api/sessions payload describing a driver-creation failure."""
    raw_capabilities = raw_attempted_capabilities(options)
    return {
        "session_id": session_id,
        "test_name": test_name,
        "status": "error",
        "requested_pack_id": pack_id,
        "requested_platform_id": infer_requested_platform_id(raw_capabilities, platform_id=platform_id),
        "requested_device_type": read_enum_capability(
            raw_capabilities,
            "appium:device_type",
            "device_type",
            allowed=KNOWN_DEVICE_TYPES,
        ),
        "requested_connection_type": read_enum_capability(
            raw_capabilities,
            "appium:connection_type",
            "connection_type",
            allowed=KNOWN_CONNECTION_TYPES,
        ),
        "requested_capabilities": raw_capabilities,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }
