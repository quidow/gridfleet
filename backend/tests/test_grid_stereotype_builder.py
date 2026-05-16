import uuid

from app.appium_nodes.services.common import build_grid_stereotype_caps
from app.devices.models import ConnectionType, Device, DeviceType


def _device(**overrides: object) -> Device:
    base: dict[str, object] = dict(
        id=uuid.UUID("75c91097-5b84-4f53-931d-1f97f1a5f35a"),
        name="Fire TV Stick 4K",
        pack_id="appium-uiautomator2",
        platform_id="firetv_real",
        identity_scheme="android_serial",
        identity_scope="host",
        os_version="6",
        manufacturer="Amazon",
        model="Fire TV Stick 4K",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="192.168.1.254",
        tags=None,
        device_config=None,
        identity_value="G070VM2011740KW1",
        connection_target="192.168.1.254:5555",
    )
    base.update(overrides)
    device = Device()
    for key, value in base.items():
        setattr(device, key, value)
    return device


def test_minimal_stereotype_carries_pack_keys_and_device_id() -> None:
    pack_stereotype = {
        "platformName": "Android",
        "appium:automationName": "UiAutomator2",
        "appium:platform": "firetv_real",
        "appium:os_version": "6",
        "appium:device_type": "real_device",
    }
    device = _device()

    caps = build_grid_stereotype_caps(device, pack_stereotype=pack_stereotype)

    assert caps["platformName"] == "Android"
    assert caps["appium:automationName"] == "UiAutomator2"
    assert caps["appium:platform"] == "firetv_real"
    assert caps["appium:os_version"] == "6"
    assert caps["appium:device_type"] == "real_device"
    assert caps["appium:gridfleet:deviceId"] == str(device.id)


def test_minimal_stereotype_drops_extra_caps_dump() -> None:
    device = _device()

    caps = build_grid_stereotype_caps(device, pack_stereotype={})

    for forbidden in (
        "appium:manufacturer",
        "appium:model",
        "appium:gridfleet:deviceName",
        "appium:ip",
    ):
        assert forbidden not in caps, f"{forbidden} should not be in routing stereotype"


def test_minimal_stereotype_drops_sanitized_device_config_appium_caps() -> None:
    device = _device(
        device_config={
            "appium_caps": {
                "appium:custom_setting": "value",
                "appium:another_setting": True,
            }
        }
    )

    caps = build_grid_stereotype_caps(device, pack_stereotype={})

    assert "appium:custom_setting" not in caps
    assert "appium:another_setting" not in caps


def test_minimal_stereotype_emits_tag_fanout() -> None:
    device = _device(tags={"rack": "A1", "screen_type": "4k"})

    caps = build_grid_stereotype_caps(device, pack_stereotype={})

    assert caps["appium:gridfleet:tag:rack"] == "A1"
    assert caps["appium:gridfleet:tag:screen_type"] == "4k"


def test_minimal_stereotype_handles_none_tags() -> None:
    device = _device(tags=None)

    caps = build_grid_stereotype_caps(device, pack_stereotype={})

    assert not any(k.startswith("appium:gridfleet:tag:") for k in caps)


def test_minimal_stereotype_device_id_wins_over_pack_collision() -> None:
    """Manager-owned deviceId sentinel beats whatever the pack stereotype declares."""
    device = _device()
    pack_stereotype = {
        "platformName": "Android",
        "appium:gridfleet:deviceId": "wrong-id",
    }

    caps = build_grid_stereotype_caps(device, pack_stereotype=pack_stereotype)

    assert caps["appium:gridfleet:deviceId"] == str(device.id)


def test_minimal_stereotype_skips_device_id_when_unset() -> None:
    """Guard for transient device rows without an id assigned yet.

    Persisted devices always have id, but the builder is called from synchronous
    payload-construction paths that operate on Device instances before flush
    can complete; emitting the literal "None" string would poison the Grid hub
    routing index.
    """
    device = _device(id=None)

    caps = build_grid_stereotype_caps(device, pack_stereotype={"platformName": "Android"})

    assert "appium:gridfleet:deviceId" not in caps
    assert caps["platformName"] == "Android"
