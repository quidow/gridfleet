from __future__ import annotations

from unittest.mock import MagicMock

from gridfleet_testkit.session import get_device_test_data_for_driver


def test_get_device_test_data_for_driver_resolves_then_fetches() -> None:
    driver = MagicMock()
    driver.capabilities = {"appium:gridfleet:deviceId": "dev-123"}

    client = MagicMock()
    client.get_device_test_data.return_value = {"flag": "x"}

    result = get_device_test_data_for_driver(driver, gridfleet_client=client)

    assert result == {"flag": "x"}
    client.get_device_test_data.assert_called_once_with("dev-123")
