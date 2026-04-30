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
