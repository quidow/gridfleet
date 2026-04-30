import uuid

from app.models.device import ConnectionType, Device, DeviceType
from app.services.node_manager_common import build_extra_caps


def _device(**overrides: object) -> Device:
    base: dict[str, object] = dict(
        id=uuid.uuid4(),
        name="Pixel 8",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="14",
        manufacturer="Google",
        model="Pixel 8",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        tags={"owner": "qa"},
        device_config=None,
        identity_value="serial-1",
        connection_target="serial-1",
    )
    base.update(overrides)
    device = Device()
    for key, value in base.items():
        setattr(device, key, value)
    return device


def test_build_extra_caps_sources_hardware_from_columns() -> None:
    caps = build_extra_caps(_device())
    assert caps["appium:platform"] == "android_mobile"
    assert caps["appium:manufacturer"] == "Google"
    assert caps["appium:model"] == "Pixel 8"
    assert caps["appium:os_version"] == "14"
    assert caps["appium:device_type"] == "real_device"
    assert "appium:device_family" not in caps
    assert "appium:fireos" not in caps
    assert "appium:android_version" not in caps


def test_build_extra_caps_skips_missing_optional_columns() -> None:
    caps = build_extra_caps(_device(manufacturer=None, model=None, os_version="unknown"))
    assert "appium:manufacturer" not in caps
    assert "appium:model" not in caps
    assert "appium:os_version" not in caps
    assert caps["appium:platform"] == "android_mobile"
