from unittest.mock import AsyncMock, patch

from agent_app.capabilities import (
    _get_tool_version,
    clear_capabilities_snapshot,
    detect_capabilities,
    get_capabilities_snapshot,
    get_or_refresh_capabilities_snapshot,
)


async def test_get_tool_version_extracts_regex_match() -> None:
    with patch("agent_app.capabilities._run_cmd", new_callable=AsyncMock, return_value="Appium 2.5.1"):
        version = await _get_tool_version("appium", ["--version"], r"(\d+\.\d+\.\d+)")

    assert version == "2.5.1"


async def test_get_tool_version_falls_back_to_first_line() -> None:
    with patch("agent_app.capabilities._run_cmd", new_callable=AsyncMock, return_value="custom-version\nignored"):
        version = await _get_tool_version("custom", ["--version"], r"no-match")

    assert version == "custom-version"


async def test_detect_capabilities_infers_platforms_from_available_tools() -> None:
    with (
        patch("agent_app.capabilities._find_appium", return_value="/node/bin/appium"),
        patch(
            "agent_app.capabilities._get_tool_version",
            new_callable=AsyncMock,
            side_effect=["2.0.0", "1.0.41", "15.0", "1.0.207"],
        ),
    ):
        capabilities = await detect_capabilities()

    assert capabilities["tools"] == {
        "appium": "2.0.0",
        "adb": "1.0.41",
        "xcodebuild": "15.0",
        "go_ios": "1.0.207",
    }
    assert capabilities["platforms"] == []
    assert capabilities["missing_prerequisites"] == []


async def test_detect_capabilities_reports_linux_missing_prerequisites_without_apple_tools() -> None:
    with (
        patch("agent_app.capabilities._find_appium", return_value="/node/bin/appium"),
        patch(
            "agent_app.capabilities._get_tool_version",
            new_callable=AsyncMock,
            side_effect=["2.0.0", None, None, None],
        ),
    ):
        capabilities = await detect_capabilities()

    assert capabilities["platforms"] == []
    assert capabilities["missing_prerequisites"] == []


async def test_detect_capabilities_does_not_require_global_appium_runtime() -> None:
    with (
        patch("agent_app.capabilities._find_appium", return_value="/node/bin/appium"),
        patch(
            "agent_app.capabilities._get_tool_version",
            new_callable=AsyncMock,
            side_effect=[None, "1.0.41", None, None],
        ),
    ):
        capabilities = await detect_capabilities()

    assert "appium" not in capabilities["missing_prerequisites"]


async def test_detect_capabilities_checks_adapter_tools_by_command_name() -> None:
    with (
        patch("agent_app.capabilities._find_appium", return_value="/node/bin/appium"),
        patch("agent_app.capabilities._get_tool_version", new_callable=AsyncMock, return_value=None) as get_version,
    ):
        await detect_capabilities()

    assert get_version.await_args_list[1].args[0] == "adb"
    assert get_version.await_args_list[3].args[0] == "ios"


async def test_capabilities_snapshot_refreshes_only_when_missing_or_forced() -> None:
    clear_capabilities_snapshot()

    first_snapshot = {"platforms": ["roku"], "tools": {"appium": "3.0.0"}, "missing_prerequisites": ["java"]}
    second_snapshot = {"platforms": ["roku"], "tools": {"appium": "3.0.1"}, "missing_prerequisites": []}
    with patch(
        "agent_app.capabilities.detect_capabilities",
        new_callable=AsyncMock,
        side_effect=[first_snapshot, second_snapshot],
    ) as detect:
        assert get_capabilities_snapshot() == {"platforms": [], "tools": {}, "missing_prerequisites": []}
        assert await get_or_refresh_capabilities_snapshot() == first_snapshot
        assert await get_or_refresh_capabilities_snapshot() == first_snapshot
        assert await get_or_refresh_capabilities_snapshot(force=True) == second_snapshot

    assert detect.await_count == 2
    clear_capabilities_snapshot()
