from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import gridfleet_testkit.session as session_mod
from gridfleet_testkit.client import GridFleetClient
from gridfleet_testkit.session import (
    get_connection_target_from_driver,
    get_device_config_for_driver,
    get_device_id_from_driver,
    resolve_device_handle_from_driver,
)

if TYPE_CHECKING:
    from gridfleet_testkit.types import JsonObject


class FakeDriver:
    def __init__(self, capabilities: dict[str, object]) -> None:
        self.session_id = "sess-1"
        self.capabilities = capabilities


# ---------------------------------------------------------------------------
# get_connection_target_from_driver / get_device_id_from_driver
# ---------------------------------------------------------------------------


def test_get_connection_target_from_driver_returns_runtime_udid() -> None:
    driver = FakeDriver({"appium:udid": "10.0.0.8:5555"})

    assert get_connection_target_from_driver(driver) == "10.0.0.8:5555"


def test_get_connection_target_from_driver_rejects_missing_udid() -> None:
    driver = FakeDriver({})

    with pytest.raises(ValueError, match="Could not determine device connection target"):
        get_connection_target_from_driver(driver)


def test_get_device_id_from_driver_returns_injected_cap() -> None:
    driver = MagicMock()
    driver.capabilities = {"appium:gridfleet:deviceId": "dev-uuid-1"}
    assert get_device_id_from_driver(driver) == "dev-uuid-1"


def test_get_device_id_from_driver_rejects_missing_cap() -> None:
    driver = MagicMock()
    driver.capabilities = {"appium:udid": "10.0.0.8:5555"}
    with pytest.raises(ValueError, match="appium:gridfleet:deviceId"):
        get_device_id_from_driver(driver)


def test_get_device_config_for_driver_uses_device_id() -> None:
    driver = FakeDriver({"appium:gridfleet:deviceId": "dev-uuid-99"})

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def get_device_config(self, device_id: str) -> JsonObject:
            self.calls.append(device_id)
            return {"device_id": device_id}

    client = FakeClient()
    assert get_device_config_for_driver(driver, gridfleet_client=client) == {
        "device_id": "dev-uuid-99",
    }
    assert client.calls == ["dev-uuid-99"]


# ---------------------------------------------------------------------------
# resolve_device_handle_from_driver
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Module surface check (was test_sessions.py)
# ---------------------------------------------------------------------------


def test_session_module_does_not_have_old_sessions_symbols() -> None:
    assert not hasattr(session_mod, "raw_attempted_capabilities")
    assert not hasattr(session_mod, "infer_requested_platform_id")
    assert not hasattr(session_mod, "read_enum_capability")
    assert not hasattr(session_mod, "KNOWN_DEVICE_TYPES")
    assert not hasattr(session_mod, "KNOWN_CONNECTION_TYPES")
    assert not hasattr(session_mod, "build_error_session_payload")
