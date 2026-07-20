from __future__ import annotations

from gridfleet_testkit.device import Device

# A realistic serialize_device-shaped row, including extra backend keys Device drops.
SERIALIZED_DEVICE = {
    "id": "11111111-1111-1111-1111-111111111111",
    "identity_value": "SERIAL123",
    "connection_target": "SERIAL123",
    "name": "Pixel 6",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "platform_label": "Android",
    "os_version": "14",
    "os_version_display": "Android 14",
    "host_id": "22222222-2222-2222-2222-222222222222",
    "device_type": "real_device",
    "connection_type": "usb",
    "manufacturer": "Google",
    "model": "Pixel 6",
    "operational_state": "available",
    "is_reserved": False,
    "device_config": {"foo": "bar"},
    "health_summary": {"overall": "ok"},
}


def test_from_payload_parses_all_curated_fields() -> None:
    device = Device.from_payload(dict(SERIALIZED_DEVICE))

    assert device == Device(
        id="11111111-1111-1111-1111-111111111111",
        identity_value="SERIAL123",
        connection_target="SERIAL123",
        name="Pixel 6",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        platform_label="Android",
        os_version="14",
        os_version_display="Android 14",
        host_id="22222222-2222-2222-2222-222222222222",
        device_type="real_device",
        connection_type="usb",
        manufacturer="Google",
        model="Pixel 6",
        operational_state="available",
        is_reserved=False,
    )


def test_from_payload_defaults_optionals_to_none_when_absent() -> None:
    device = Device.from_payload(
        {
            "id": "dev-1",
            "identity_value": "SERIAL123",
            "name": "Pixel 6",
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "os_version": "14",
            "host_id": "host-1",
            "device_type": "real_device",
            "connection_type": "usb",
            "operational_state": "available",
            "is_reserved": False,
        }
    )

    assert device.connection_target is None
    assert device.platform_label is None
    assert device.os_version_display is None
    assert device.manufacturer is None
    assert device.model is None


def test_from_payload_drops_retired_tags() -> None:
    device = Device.from_payload({"id": "dev-1", "tags": {"lab": "east"}})
    assert not hasattr(device, "tags")


def test_from_payload_ignores_unknown_backend_keys() -> None:
    device = Device.from_payload({"id": "dev-1", "telemetry": {"x": 1}, "device_config": {}})

    assert device.id == "dev-1"
    assert device.is_reserved is False
