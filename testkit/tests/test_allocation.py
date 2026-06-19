from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from gridfleet_testkit.allocation import (
    AllocatedDevice,
    hydrate_allocated_device,
    hydrate_allocated_device_from_driver,
)

if TYPE_CHECKING:
    from gridfleet_testkit.types import JsonObject


class FakeClient:
    def __init__(self) -> None:
        self.capability_calls: list[str] = []
        self.device_calls: list[str] = []
        self.test_data_calls: list[str] = []

    def get_device_capabilities(self, device_id: str) -> JsonObject:
        self.capability_calls.append(device_id)
        return {"appium:udid": "SERIAL123", "appium:deviceIP": "10.0.0.9"}

    def get_device(self, device_id: str) -> JsonObject:
        self.device_calls.append(device_id)
        return {
            "id": device_id,
            "name": "Pixel 6",
            "device_type": "real_device",
            "connection_type": "usb",
            "manufacturer": "Google",
            "model": "Pixel 6",
            "ip_address": "10.0.0.7",
        }

    def get_device_test_data(self, device_id: str) -> JsonObject:
        self.test_data_calls.append(device_id)
        return {"fetched": True}


def device_handle(**overrides: object) -> JsonObject:
    payload: JsonObject = {
        "device_id": "dev-1",
        "identity_value": "SERIAL123",
        "connection_target": "SERIAL123",
        "name": "Pixel 6",
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "platform_label": "Android",
        "os_version": "14",
        "host_ip": "192.168.1.10",
        "device_type": "real_device",
        "connection_type": "usb",
        "manufacturer": "Google",
        "model": "Pixel 6",
    }
    payload.update(overrides)
    return payload


def test_hydrate_allocated_device_uses_device_handle() -> None:
    client = FakeClient()

    allocated = hydrate_allocated_device(device_handle(), run_id="run-1", client=client)

    assert allocated == AllocatedDevice(
        run_id="run-1",
        device_id="dev-1",
        identity_value="SERIAL123",
        name="Pixel 6",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        platform_label="Android",
        os_version="14",
        connection_target="SERIAL123",
        host_ip="192.168.1.10",
        device_type="real_device",
        connection_type="usb",
        manufacturer="Google",
        model="Pixel 6",
        live_capabilities=None,
        test_data=None,
    )
    assert client.capability_calls == []
    assert client.device_calls == []


def test_hydrate_allocated_device_fetches_device_detail_when_handle_lacks_richer_fields() -> None:
    client = FakeClient()
    payload = device_handle(name=None, device_type=None, connection_type=None, manufacturer=None, model=None)
    del payload["name"]
    del payload["device_type"]
    del payload["connection_type"]
    del payload["manufacturer"]
    del payload["model"]

    allocated = hydrate_allocated_device(payload, run_id="run-1", client=client)

    assert allocated.name == "Pixel 6"
    assert allocated.device_type == "real_device"
    assert allocated.connection_type == "usb"
    assert allocated.manufacturer == "Google"
    assert allocated.model == "Pixel 6"
    assert client.device_calls == ["dev-1"]


def test_hydrate_allocated_device_requires_device_id() -> None:
    client = FakeClient()
    payload = device_handle()
    del payload["device_id"]

    with pytest.raises(ValueError, match="Allocated device payload is missing device_id"):
        hydrate_allocated_device(payload, run_id="run-1", client=client)


def test_hydrate_allocated_device_fetches_capabilities_when_requested() -> None:
    client = FakeClient()

    allocated = hydrate_allocated_device(
        device_handle(),
        run_id="run-1",
        client=client,
        fetch_capabilities=True,
    )

    assert allocated.live_capabilities == {"appium:udid": "SERIAL123", "appium:deviceIP": "10.0.0.9"}
    assert client.capability_calls == ["dev-1"]


def test_allocated_device_properties_prefer_stable_sources() -> None:
    allocated = AllocatedDevice(
        run_id="run-1",
        device_id="dev-1",
        identity_value="SERIAL123",
        name="Pixel 6",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        platform_label="Android",
        os_version="14",
        connection_target=None,
        host_ip=None,
        device_type="emulator",
        connection_type="virtual",
        manufacturer=None,
        model=None,
        live_capabilities={"appium:udid": "emulator-5554", "appium:deviceIP": "10.0.0.9"},
        test_data=None,
    )

    assert allocated.is_real_device is False
    assert allocated.is_simulator is True
    assert allocated.udid == "emulator-5554"
    assert allocated.device_ip == "10.0.0.9"
    assert allocated.platform_name == "Android"


def test_hydrate_allocated_device_from_driver_returns_new_frozen_instance() -> None:
    client = FakeClient()
    allocated = hydrate_allocated_device(device_handle(), run_id="run-1", client=client)
    driver = type("Driver", (), {"capabilities": {"appium:udid": "SERIAL123", "platformName": "Android"}})()

    updated = hydrate_allocated_device_from_driver(allocated, driver, client=client)

    assert updated is not allocated
    assert updated.live_capabilities == {"appium:udid": "SERIAL123", "platformName": "Android"}
    assert allocated.live_capabilities is None


def test_hydrate_allocated_device_populates_inline_tags() -> None:
    client = FakeClient()
    allocated = hydrate_allocated_device(
        device_handle(tags={"screen_type": "4k"}),
        run_id="run-1",
        client=client,
    )
    assert allocated.tags == {"screen_type": "4k"}


def test_hydrate_defaults_test_data_to_none_when_absent() -> None:
    client = FakeClient()
    allocated = hydrate_allocated_device(
        device_handle(),
        run_id="run-1",
        client=client,
    )
    assert allocated.test_data is None
    assert client.test_data_calls == []


def test_hydrate_fetches_test_data_when_flag_enabled() -> None:
    client = FakeClient()
    allocated = hydrate_allocated_device(
        device_handle(),
        run_id="run-1",
        client=client,
        fetch_test_data=True,
    )
    assert allocated.test_data == {"fetched": True}
    assert client.test_data_calls == ["dev-1"]
