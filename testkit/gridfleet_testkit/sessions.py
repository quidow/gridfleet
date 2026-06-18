"""Public session payload helpers for GridFleet integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .appium import get_device_id_from_driver

if TYPE_CHECKING:
    from appium.webdriver.webdriver import WebDriver

    from .client import GridFleetClient
    from .types import JsonObject

__all__ = ["resolve_device_handle_from_driver"]


def resolve_device_handle_from_driver(driver: WebDriver, *, client: GridFleetClient) -> JsonObject:
    """Resolve a canonical device handle from a running WebDriver session."""
    return client.get_device(get_device_id_from_driver(driver))
