"""Boundary constraints on incoming request schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_app.appium.schemas import AppiumStartRequest, AppiumStopRequest
from agent_app.pack.schemas import FeatureActionRequest, NormalizeDeviceRequest
from agent_app.plugins.schemas import PluginConfig


def _valid_start_payload() -> dict[str, object]:
    return {
        "connection_target": "device-123",
        "port": 4723,
        "grid_url": "http://hub:4444",
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


def test_feature_action_rejects_blank_pack_id() -> None:
    with pytest.raises(ValidationError):
        FeatureActionRequest(pack_id="", args={})


def test_feature_action_rejects_pack_id_with_path_traversal() -> None:
    with pytest.raises(ValidationError):
        FeatureActionRequest(pack_id="../etc/passwd", args={})


def test_feature_action_accepts_valid_pack_id() -> None:
    FeatureActionRequest(pack_id="appium-uiautomator2", args={})


def test_feature_action_accepts_namespaced_pack_id() -> None:
    FeatureActionRequest(pack_id="local/uiautomator2-android-real", args={})


def test_feature_action_rejects_pack_id_with_double_dot_segment() -> None:
    with pytest.raises(ValidationError):
        FeatureActionRequest(pack_id="local/../etc", args={})


def test_feature_action_rejects_pack_id_with_trailing_slash() -> None:
    with pytest.raises(ValidationError):
        FeatureActionRequest(pack_id="foo/", args={})


def test_feature_action_rejects_pack_id_with_leading_slash() -> None:
    with pytest.raises(ValidationError):
        FeatureActionRequest(pack_id="/foo", args={})


def test_feature_action_rejects_pack_id_with_double_slash() -> None:
    with pytest.raises(ValidationError):
        FeatureActionRequest(pack_id="foo//bar", args={})


def test_plugin_config_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError):
        PluginConfig(name="../etc/passwd", version="1.0.0", source="npm")


def test_plugin_config_rejects_double_dot() -> None:
    with pytest.raises(ValidationError):
        PluginConfig(name="..", version="1.0.0", source="npm")


def test_plugin_config_rejects_uppercase() -> None:
    with pytest.raises(ValidationError):
        PluginConfig(name="UPPERCASE-PLUGIN", version="1.0.0", source="npm")


def test_plugin_config_rejects_leading_underscore() -> None:
    with pytest.raises(ValidationError):
        PluginConfig(name="_hidden", version="1.0.0", source="npm")


def test_plugin_config_accepts_scoped_name() -> None:
    PluginConfig(name="@appium/plugin-name", version="1.0.0", source="npm")


def test_plugin_config_accepts_plain_name() -> None:
    PluginConfig(name="appium-uiautomator2-driver", version="1.0.0", source="npm")
