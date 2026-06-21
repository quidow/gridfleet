"""Helpers that read GridFleet/Appium state from a live WebDriver session."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .client import GridFleetClient

if TYPE_CHECKING:
    from appium.webdriver.webdriver import WebDriver

    from .device import Device
    from .types import JsonObject


def _required_str_cap(driver: WebDriver, key: str, message: str) -> str:
    """Return a required non-empty string session capability, or raise ValueError."""
    capabilities = cast("JsonObject", driver.capabilities) if isinstance(driver.capabilities, dict) else {}
    value = capabilities.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(message)
    return value


def get_device_id_from_driver(driver: WebDriver) -> str:
    """Return the GridFleet device id from a live Appium driver's session caps."""
    return _required_str_cap(
        driver,
        "gridfleet:deviceId",
        "Session capabilities are missing 'gridfleet:deviceId'; the GridFleet router must be new enough to inject it",
    )


def get_device_test_data_for_driver(
    driver: WebDriver,
    *,
    gridfleet_client: GridFleetClient | None = None,
) -> JsonObject:
    """Fetch operator-attached test_data for a live Appium driver session."""
    client = gridfleet_client or GridFleetClient()
    device_id = get_device_id_from_driver(driver)
    return client.get_device_test_data(device_id)


def resolve_device_handle_from_driver(driver: WebDriver, *, client: GridFleetClient) -> Device:
    """Resolve the assigned manager device row as a typed ``Device`` from a running session."""
    return client.get_device(get_device_id_from_driver(driver))
