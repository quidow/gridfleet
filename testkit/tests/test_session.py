from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

from gridfleet_testkit.client import GridFleetClient
from gridfleet_testkit.device import Device
from gridfleet_testkit.session import (
    get_device_id_from_driver,
    resolve_device_handle_from_driver,
)

# ---------------------------------------------------------------------------
# get_device_id_from_driver
# ---------------------------------------------------------------------------


def test_get_device_id_from_driver_returns_injected_cap() -> None:
    driver = MagicMock()
    driver.capabilities = {"appium:gridfleet:deviceId": "dev-uuid-1"}
    assert get_device_id_from_driver(driver) == "dev-uuid-1"


def test_get_device_id_from_driver_rejects_missing_cap() -> None:
    driver = MagicMock()
    driver.capabilities = {"appium:udid": "10.0.0.8:5555"}
    with pytest.raises(ValueError, match="appium:gridfleet:deviceId"):
        get_device_id_from_driver(driver)


# ---------------------------------------------------------------------------
# resolve_device_handle_from_driver
# ---------------------------------------------------------------------------


def test_resolves_handle_via_device_id() -> None:
    fake_driver = MagicMock()
    fake_driver.capabilities = {"appium:gridfleet:deviceId": "device-uuid"}

    fake_client = MagicMock(spec=GridFleetClient)
    fake_client.get_device.return_value = Device.from_payload(
        {
            "id": "device-uuid",
            "connection_target": "R58M111",
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
        }
    )

    handle = resolve_device_handle_from_driver(fake_driver, client=fake_client)

    assert handle.id == "device-uuid"
    fake_client.get_device.assert_called_once_with("device-uuid")


# ---------------------------------------------------------------------------
# Module surface check (was test_sessions.py)
# ---------------------------------------------------------------------------


def test_session_module_does_not_have_old_sessions_symbols() -> None:
    session_mod = sys.modules["gridfleet_testkit.session"]
    assert not hasattr(session_mod, "raw_attempted_capabilities")
    assert not hasattr(session_mod, "infer_requested_platform_id")
    assert not hasattr(session_mod, "read_enum_capability")
    assert not hasattr(session_mod, "KNOWN_DEVICE_TYPES")
    assert not hasattr(session_mod, "KNOWN_CONNECTION_TYPES")
    assert not hasattr(session_mod, "build_error_session_payload")
