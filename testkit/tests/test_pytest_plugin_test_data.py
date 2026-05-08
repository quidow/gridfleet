from __future__ import annotations

from unittest.mock import MagicMock


def test_get_device_test_data_for_driver_resolves_then_fetches() -> None:
    from gridfleet_testkit.appium import get_device_test_data_for_driver

    driver = MagicMock()
    driver.capabilities = {"appium:udid": "udid-123"}

    client = MagicMock()
    client.resolve_device_id_by_connection_target.return_value = "dev-123"
    client.get_device_test_data.return_value = {"flag": "x"}

    result = get_device_test_data_for_driver(driver, gridfleet_client=client)

    assert result == {"flag": "x"}
    client.resolve_device_id_by_connection_target.assert_called_once_with("udid-123")
    client.get_device_test_data.assert_called_once_with("dev-123")
