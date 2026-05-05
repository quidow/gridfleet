from __future__ import annotations

from typing import Any

import pytest

from gridfleet_testkit.allocation import (
    AllocatedDevice,
    UnavailableInclude,
    hydrate_allocated_device,
    hydrate_allocated_device_from_driver,
)


class FakeClient:
    def __init__(self) -> None:
        self.config_calls: list[tuple[str, bool]] = []
        self.capability_calls: list[str] = []
        self.device_calls: list[str] = []

    def get_device_config(self, connection_target: str, reveal: bool = True) -> dict[str, Any]:
        self.config_calls.append((connection_target, reveal))
        return {"ip": "10.0.0.8", "username": "operator"}

    def get_device_capabilities(self, device_id: str) -> dict[str, Any]:
        self.capability_calls.append(device_id)
        return {"appium:udid": "SERIAL123", "appium:deviceIP": "10.0.0.9"}

    def get_device(self, device_id: str) -> dict[str, Any]:
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


def claim_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
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
        "claimed_by": "gw0",
        "claimed_at": "2026-05-05T10:00:00Z",
    }
    payload.update(overrides)
    return payload


def test_hydrate_allocated_device_uses_claim_payload_and_fetches_config() -> None:
    client = FakeClient()

    allocated = hydrate_allocated_device(claim_payload(), run_id="run-1", client=client)

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
        claimed_by="gw0",
        claimed_at="2026-05-05T10:00:00Z",
        config={"ip": "10.0.0.8", "username": "operator"},
        live_capabilities=None,
    )
    assert client.config_calls == [("SERIAL123", True)]
    assert client.capability_calls == []
    assert client.device_calls == []


def test_hydrate_allocated_device_fetches_device_detail_when_claim_payload_lacks_richer_fields() -> None:
    client = FakeClient()
    payload = claim_payload(name=None, device_type=None, connection_type=None, manufacturer=None, model=None)
    del payload["name"]
    del payload["device_type"]
    del payload["connection_type"]
    del payload["manufacturer"]
    del payload["model"]

    allocated = hydrate_allocated_device(payload, run_id="run-1", client=client, fetch_config=False)

    assert allocated.name == "Pixel 6"
    assert allocated.device_type == "real_device"
    assert allocated.connection_type == "usb"
    assert allocated.manufacturer == "Google"
    assert allocated.model == "Pixel 6"
    assert client.device_calls == ["dev-1"]
    assert client.config_calls == []


def test_hydrate_allocated_device_requires_device_id() -> None:
    client = FakeClient()
    payload = claim_payload()
    del payload["device_id"]

    with pytest.raises(ValueError, match="Allocated device payload is missing device_id"):
        hydrate_allocated_device(payload, run_id="run-1", client=client)


def test_hydrate_allocated_device_fetches_capabilities_when_requested() -> None:
    client = FakeClient()

    allocated = hydrate_allocated_device(
        claim_payload(),
        run_id="run-1",
        client=client,
        fetch_config=False,
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
        claimed_by="gw0",
        claimed_at="2026-05-05T10:00:00Z",
        config={"ip": "10.0.0.8"},
        live_capabilities={"appium:udid": "emulator-5554", "appium:deviceIP": "10.0.0.9"},
    )

    assert allocated.is_real_device is False
    assert allocated.is_simulator is True
    assert allocated.udid == "emulator-5554"
    assert allocated.device_ip == "10.0.0.9"
    assert allocated.platform_name == "Android"


def test_allocated_device_defaults_unavailable_includes_and_config_is_masked() -> None:
    allocated = AllocatedDevice(
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
        claimed_by="gw0",
        claimed_at="2026-05-05T10:00:00Z",
        config=None,
        live_capabilities=None,
    )

    assert allocated.unavailable_includes == ()
    assert isinstance(allocated.unavailable_includes, tuple)
    assert all(isinstance(u, UnavailableInclude) for u in allocated.unavailable_includes)
    assert allocated.config_is_masked is False


def test_hydrate_allocated_device_uses_inline_config_and_skips_get() -> None:
    client = FakeClient()
    payload = claim_payload(config={"ip": "10.0.0.8", "username": "operator", "password": "********"})

    allocated = hydrate_allocated_device(payload, run_id="run-1", client=client)

    assert allocated.config == {"ip": "10.0.0.8", "username": "operator", "password": "********"}
    assert allocated.config_is_masked is True
    assert client.config_calls == []
    assert client.device_calls == []


def test_hydrate_allocated_device_falls_back_to_get_device_config_when_inline_absent() -> None:
    client = FakeClient()

    allocated = hydrate_allocated_device(claim_payload(), run_id="run-1", client=client)

    assert allocated.config == {"ip": "10.0.0.8", "username": "operator"}
    assert allocated.config_is_masked is False
    assert client.config_calls == [("SERIAL123", True)]


def test_hydrate_allocated_device_from_driver_returns_new_frozen_instance() -> None:
    client = FakeClient()
    allocated = hydrate_allocated_device(claim_payload(), run_id="run-1", client=client, fetch_config=False)
    driver = type("Driver", (), {"capabilities": {"appium:udid": "SERIAL123", "platformName": "Android"}})()

    updated = hydrate_allocated_device_from_driver(allocated, driver, client=client)

    assert updated is not allocated
    assert updated.live_capabilities == {"appium:udid": "SERIAL123", "platformName": "Android"}
    assert allocated.live_capabilities is None
