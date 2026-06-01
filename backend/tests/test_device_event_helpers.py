from app.devices.services.event import build_device_crashed_payload


def test_build_device_crashed_payload_shape() -> None:
    payload = build_device_crashed_payload(
        device_id="d1",
        device_name="pixel",
        source="appium_crash",
        reason="boom",
        will_restart=True,
        process="appium",
    )
    assert payload == {
        "device_id": "d1",
        "device_name": "pixel",
        "source": "appium_crash",
        "reason": "boom",
        "will_restart": True,
        "process": "appium",
    }
