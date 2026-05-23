from __future__ import annotations

import inspect

from adapter import Adapter

REQUIRED_METHODS = {
    "discover",
    "doctor",
    "health_check",
    "lifecycle_action",
    "pre_session",
    "post_session",
    "normalize_device",
    "telemetry",
    "feature_action",
    "sidecar_lifecycle",
}


def test_adapter_implements_all_protocol_methods() -> None:
    methods = {name for name, _ in inspect.getmembers(Adapter, predicate=inspect.isfunction)}
    missing = REQUIRED_METHODS - methods
    assert not missing, f"Adapter missing methods: {missing}"


def test_adapter_attributes() -> None:
    adapter = Adapter()
    adapter.pack_id = "appium-uiautomator2"
    adapter.pack_release = "0.1.0"
    assert adapter.pack_id == "appium-uiautomator2"


def test_subprocess_env_returns_adb_and_android_home() -> None:
    from unittest.mock import patch

    from agent_app.pack.adapter_types import SubprocessEnvContribution

    with (
        patch("adapter.tools.find_adb", return_value="/opt/android/platform-tools/adb"),
        patch("adapter.tools.find_android_home", return_value="/opt/android"),
    ):
        result = Adapter().subprocess_env()

    assert isinstance(result, SubprocessEnvContribution)
    assert result.env_vars == {"ANDROID_HOME": "/opt/android", "ANDROID_SDK_ROOT": "/opt/android"}
    assert result.extra_path_dirs == ["/opt/android/platform-tools"]


def test_subprocess_env_no_adb_found() -> None:
    from unittest.mock import patch

    from agent_app.pack.adapter_types import SubprocessEnvContribution

    with (
        patch("adapter.tools.find_adb", return_value="adb"),
        patch("adapter.tools.find_android_home", return_value=None),
    ):
        result = Adapter().subprocess_env()

    assert isinstance(result, SubprocessEnvContribution)
    assert result.env_vars == {}
    assert result.extra_path_dirs == []
