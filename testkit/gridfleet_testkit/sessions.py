"""Public session payload helpers for GridFleet integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from appium.webdriver.webdriver import WebDriver

    from .client import GridFleetClient
    from .types import JsonObject

__all__ = ["resolve_device_handle_from_driver"]


def resolve_device_handle_from_driver(driver: WebDriver, *, client: GridFleetClient) -> JsonObject:
    """Resolve a canonical device handle from a running WebDriver session."""
    caps = getattr(driver, "capabilities", None) or {}
    if not isinstance(caps, dict):
        raise RuntimeError("driver capabilities missing appium:udid; cannot resolve device handle")
    target = caps.get("appium:udid") or caps.get("appium:deviceName")
    if not target:
        raise RuntimeError("driver capabilities missing appium:udid; cannot resolve device handle")
    return client.get_device_by_connection_target(str(target))
