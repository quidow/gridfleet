from __future__ import annotations

import inspect
from unittest.mock import patch

import pytest
from adapter import Adapter

REQUIRED_METHODS = {
    "discover",
    "doctor",
    "health_check",
    "pre_session",
    "post_session",
    "normalize_device",
}


def test_adapter_protocol() -> None:
    methods = {name for name, _ in inspect.getmembers(Adapter, predicate=inspect.isfunction)}
    assert REQUIRED_METHODS.issubset(methods)


@pytest.mark.asyncio
@patch("adapter.tools.host_supports_apple_devicectl", return_value=True)
async def test_doctor_requires_devicectl(_mock_devicectl: object) -> None:
    checks = await Adapter().doctor(None)

    assert [(check.check_id, check.ok) for check in checks] == [
        ("xcrun", True),
    ]


def test_tool_versions_returns_xcodebuild() -> None:
    from unittest.mock import patch

    from adapter import Adapter

    xcode_result = type("R", (), {"stdout": "Xcode 16.2\nBuild version 16C5032a", "returncode": 0})()

    with patch("subprocess.run", side_effect=[xcode_result]):
        result = Adapter().tool_versions()

    assert result == {"xcodebuild": "16.2"}


def test_tool_versions_handles_missing_tools() -> None:
    from unittest.mock import patch

    from adapter import Adapter

    with patch("subprocess.run", side_effect=FileNotFoundError()):
        result = Adapter().tool_versions()

    assert result == {"xcodebuild": None}