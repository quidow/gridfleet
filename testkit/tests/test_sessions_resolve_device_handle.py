from __future__ import annotations

from unittest.mock import MagicMock

from gridfleet_testkit.client import GridFleetClient
from gridfleet_testkit.sessions import resolve_device_handle_from_driver


def test_resolves_handle_via_device_id() -> None:
    fake_driver = MagicMock()
    fake_driver.capabilities = {"appium:gridfleet:deviceId": "device-uuid"}

    fake_client = MagicMock(spec=GridFleetClient)
    fake_client.get_device.return_value = {
        "id": "device-uuid",
        "connection_target": "R58M111",
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
    }

    handle = resolve_device_handle_from_driver(fake_driver, client=fake_client)

    assert handle["id"] == "device-uuid"
    fake_client.get_device.assert_called_once_with("device-uuid")
