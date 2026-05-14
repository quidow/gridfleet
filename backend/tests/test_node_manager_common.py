import uuid

from app.appium_nodes.services.common import build_extra_caps, build_grid_stereotype_caps
from app.devices.models import ConnectionType, Device, DeviceType


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


def test_build_grid_stereotype_caps_includes_tag_caps() -> None:
    device = _device(tags={"screen_type": "4k", "rack": "A1"})

    caps = build_grid_stereotype_caps(device)

    assert caps["appium:gridfleet:tag:screen_type"] == "4k"
    assert caps["appium:gridfleet:tag:rack"] == "A1"


def test_build_grid_stereotype_caps_keeps_db_tags_authoritative() -> None:
    device = _device(
        tags={"screen_type": "4k"},
        device_config={"appium_caps": {"appium:gridfleet:tag:screen_type": "hd"}},
    )

    caps = build_grid_stereotype_caps(device)

    assert caps["appium:gridfleet:tag:screen_type"] == "4k"
