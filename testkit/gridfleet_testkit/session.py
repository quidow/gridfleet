"""Helpers that read GridFleet/Appium state from a live WebDriver session."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .client import GridFleetClient

if TYPE_CHECKING:
    from appium.webdriver.webdriver import WebDriver

    from .types import JsonObject


def _required_str_cap(driver: WebDriver, key: str, message: str) -> str:
    """Return a required non-empty string session capability, or raise ValueError."""
    capabilities = cast("JsonObject", driver.capabilities) if isinstance(driver.capabilities, dict) else {}
    value = capabilities.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(message)
    return value


def get_connection_target_from_driver(driver: WebDriver) -> str:
    """Return the runtime connection target from a live Appium driver."""
    return _required_str_cap(
        driver, "appium:udid", "Could not determine device connection target from session capabilities"
    )


def get_device_id_from_driver(driver: WebDriver) -> str:
    """Return the GridFleet device id from a live Appium driver's session caps."""
    return _required_str_cap(
        driver,
        "appium:gridfleet:deviceId",
        "Session capabilities are missing 'appium:gridfleet:deviceId'; "
        "the GridFleet router must be new enough to inject it",
    )


def get_device_config_for_driver(
    driver: WebDriver,
    *,
    gridfleet_client: GridFleetClient | None = None,
) -> JsonObject:
    """Fetch device config for a live Appium driver using its GridFleet device id."""
    client = gridfleet_client or GridFleetClient()
    return client.get_device_config(get_device_id_from_driver(driver))


def get_device_test_data_for_driver(
    driver: WebDriver,
    *,
    gridfleet_client: GridFleetClient | None = None,
) -> JsonObject:
    """Fetch operator-attached test_data for a live Appium driver session."""
    client = gridfleet_client or GridFleetClient()
    device_id = get_device_id_from_driver(driver)
    return client.get_device_test_data(device_id)


def resolve_device_handle_from_driver(driver: WebDriver, *, client: GridFleetClient) -> JsonObject:
    """Resolve a canonical device handle from a running WebDriver session."""
    return client.get_device(get_device_id_from_driver(driver))
