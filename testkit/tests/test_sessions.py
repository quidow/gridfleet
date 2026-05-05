from __future__ import annotations

from typing import Any

from gridfleet_testkit.sessions import build_error_session_payload


class FakeOptions:
    def __init__(self, capabilities: dict[str, Any], platform_name: str | None = None) -> None:
        self.capabilities = capabilities
        self.platform_name = platform_name


def test_build_error_session_payload_uses_explicit_pack_and_platform() -> None:
    options = FakeOptions(
        {
            "appium:automationName": "UiAutomator2",
            "appium:device_type": "real_device",
            "appium:connection_type": "usb",
        },
        platform_name="Android",
    )

    payload = build_error_session_payload(
        session_id="error-1",
        test_name="test_login",
        options=options,
        exc=RuntimeError("Session could not be created"),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    assert payload == {
        "session_id": "error-1",
        "test_name": "test_login",
        "status": "error",
        "requested_pack_id": "appium-uiautomator2",
        "requested_platform_id": "android_mobile",
        "requested_device_type": "real_device",
        "requested_connection_type": "usb",
        "requested_capabilities": {
            "appium:automationName": "UiAutomator2",
            "appium:device_type": "real_device",
            "appium:connection_type": "usb",
            "platformName": "Android",
        },
        "error_type": "RuntimeError",
        "error_message": "Session could not be created",
    }


def test_build_error_session_payload_infers_platform_from_capability() -> None:
    options = FakeOptions({"appium:platform": "ios_simulator", "appium:connection_type": "virtual"})

    payload = build_error_session_payload(
        session_id="error-2",
        test_name="test_ios",
        options=options,
        exc=ValueError("bad caps"),
    )

    assert payload["requested_platform_id"] == "ios_simulator"
    assert payload["requested_connection_type"] == "virtual"
    assert payload["requested_device_type"] is None


def test_build_error_session_payload_ignores_unknown_enum_values() -> None:
    options = FakeOptions({"device_type": "browser", "connection_type": "serial"})

    payload = build_error_session_payload(
        session_id="error-3",
        test_name="test_unknown",
        options=options,
        exc=Exception("boom"),
    )

    assert payload["requested_device_type"] is None
    assert payload["requested_connection_type"] is None
