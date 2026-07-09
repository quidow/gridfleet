"""Boundary constraints on incoming request schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_app.appium.schemas import AppiumStartRequest, AppiumStopRequest
from agent_app.pack.schemas import NormalizeDeviceRequest


def _valid_start_payload() -> dict[str, object]:
    return {
        "connection_target": "device-123",
        "port": 4723,
        "pack_id": "appium-uiautomator2",
        "platform_id": "android",
    }


def test_appium_start_rejects_port_below_1024() -> None:
    payload = _valid_start_payload() | {"port": 80}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_rejects_port_above_65535() -> None:
    payload = _valid_start_payload() | {"port": 70000}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_rejects_blank_connection_target() -> None:
    payload = _valid_start_payload() | {"connection_target": ""}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_rejects_pack_id_with_path_traversal() -> None:
    payload = _valid_start_payload() | {"pack_id": "../etc/passwd"}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_accepts_namespaced_pack_id() -> None:
    payload = _valid_start_payload() | {"pack_id": "local/uiautomator2-android-real"}
    AppiumStartRequest(**payload)


def test_appium_start_accepts_two_segment_pack_id() -> None:
    payload = _valid_start_payload() | {"pack_id": "acme/my-custom-driver"}
    AppiumStartRequest(**payload)


def test_appium_start_rejects_pack_id_with_double_dot_segment() -> None:
    payload = _valid_start_payload() | {"pack_id": "local/../etc"}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_rejects_pack_id_with_trailing_slash() -> None:
    payload = _valid_start_payload() | {"pack_id": "foo/"}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_rejects_pack_id_with_leading_slash() -> None:
    payload = _valid_start_payload() | {"pack_id": "/foo"}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_rejects_pack_id_with_double_slash() -> None:
    payload = _valid_start_payload() | {"pack_id": "foo//bar"}
    with pytest.raises(ValidationError):
        AppiumStartRequest(**payload)


def test_appium_start_accepts_minimal_valid_payload() -> None:
    AppiumStartRequest(**_valid_start_payload())


def test_appium_stop_rejects_port_below_1024() -> None:
    with pytest.raises(ValidationError):
        AppiumStopRequest(port=80)


def test_appium_stop_rejects_port_above_65535() -> None:
    with pytest.raises(ValidationError):
        AppiumStopRequest(port=70000)


def test_normalize_device_rejects_blank_pack_id() -> None:
    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(pack_id="", pack_release="1.0.0", platform_id="android", raw_input={})


def test_normalize_device_rejects_platform_id_with_pattern_violation() -> None:
    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(
            pack_id="appium-uiautomator2",
            pack_release="1.0.0",
            platform_id="bad id with spaces",
            raw_input={},
        )


def test_normalize_device_accepts_namespaced_pack_id() -> None:
    NormalizeDeviceRequest(
        pack_id="local/uiautomator2-android-real",
        pack_release="1.0.0",
        platform_id="android",
        raw_input={},
    )


def test_normalize_device_rejects_pack_id_with_double_dot_segment() -> None:
    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(
            pack_id="local/../etc",
            pack_release="1.0.0",
            platform_id="android",
            raw_input={},
        )


def test_normalize_device_rejects_pack_id_with_trailing_slash() -> None:
    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(pack_id="foo/", pack_release="1.0.0", platform_id="android", raw_input={})


def test_normalize_device_rejects_pack_id_with_leading_slash() -> None:
    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(pack_id="/foo", pack_release="1.0.0", platform_id="android", raw_input={})


def test_normalize_device_rejects_pack_id_with_double_slash() -> None:
    with pytest.raises(ValidationError):
        NormalizeDeviceRequest(pack_id="foo//bar", pack_release="1.0.0", platform_id="android", raw_input={})


def test_appium_stop_accepts_valid_port() -> None:
    AppiumStopRequest(port=4723)


def test_normalize_device_accepts_valid_payload() -> None:
    NormalizeDeviceRequest(
        pack_id="appium-uiautomator2",
        pack_release="1.0.0",
        platform_id="android",
        raw_input={},
    )


def test_appium_start_accepts_three_dot_pack_id_segment() -> None:
    _valid_start_payload_with_three_dots = _valid_start_payload() | {"pack_id": "..."}
    AppiumStartRequest(**_valid_start_payload_with_three_dots)
