from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest
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


def test_adapter_protocol() -> None:
    methods = {name for name, _ in inspect.getmembers(Adapter, predicate=inspect.isfunction)}
    assert REQUIRED_METHODS.issubset(methods)


@pytest.mark.asyncio
@patch("adapter.tools.host_supports_apple_devicectl", return_value=True)
@patch("adapter.tools.find_go_ios", return_value="/usr/local/bin/ios")
async def test_doctor_requires_devicectl_and_go_ios(_mock_go_ios: object, _mock_devicectl: object) -> None:
    checks = await Adapter().doctor(None)

    assert [(check.check_id, check.ok) for check in checks] == [
        ("xcrun", True),
        ("go_ios", True),
    ]
