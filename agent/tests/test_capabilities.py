import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

from agent_app.capabilities import (
    _get_tool_version,
    _run_cmd,
    capabilities_refresh_loop,
    clear_capabilities_snapshot,
    detect_capabilities,
    get_capabilities_snapshot,
    get_or_refresh_capabilities_snapshot,
)


async def test_get_tool_version_extracts_regex_match() -> None:
    with patch("agent_app.capabilities._run_cmd", new_callable=AsyncMock, return_value="adb version 1.0.41"):
        version = await _get_tool_version("adb", ["--version"], r"(\d+\.\d+\.\d+)")

    assert version == "1.0.41"


async def test_get_tool_version_falls_back_to_first_line() -> None:
    with patch("agent_app.capabilities._run_cmd", new_callable=AsyncMock, return_value="custom-version\nignored"):
        version = await _get_tool_version("custom", ["--version"], r"no-match")

    assert version == "custom-version"


async def test_get_tool_version_returns_none_when_run_cmd_fails() -> None:
    with patch("agent_app.capabilities._run_cmd", new_callable=AsyncMock, return_value=None):
        version = await _get_tool_version("adb", ["--version"], r"(\d+\.\d+\.\d+)")

    assert version is None


async def test_run_cmd_returns_none_on_filenotfound() -> None:
    with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError()):
        result = await _run_cmd("nonexistent", "--version")
    assert result is None


async def test_run_cmd_returns_none_on_timeout() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(side_effect=TimeoutError())
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
        result = await _run_cmd("slow", "--version")
    assert result is None


async def test_run_cmd_returns_none_on_nonzero_exit() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"", b"error"))
    proc.returncode = 1
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
        result = await _run_cmd("failing", "--version")
    assert result is None


async def test_run_cmd_returns_stdout() -> None:
    proc = AsyncMock()
    proc.communicate = AsyncMock(return_value=(b"stdout\n", b""))
    proc.returncode = 0
    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=proc):
        result = await _run_cmd("ok", "--version")
    assert result == "stdout"


async def test_detect_capabilities_infers_platforms_from_available_tools() -> None:
    with patch(
        "agent_app.capabilities._get_tool_version",
        new_callable=AsyncMock,
        side_effect=["1.0.41", "15.0", "1.0.207"],
    ):
        capabilities = await detect_capabilities()

    assert capabilities["tools"] == {
        "adb": "1.0.41",
        "xcodebuild": "15.0",
        "go_ios": "1.0.207",
    }
    assert capabilities["platforms"] == []
    assert capabilities["missing_prerequisites"] == []


async def test_detect_capabilities_reports_linux_missing_prerequisites_without_apple_tools() -> None:
    with patch(
        "agent_app.capabilities._get_tool_version",
        new_callable=AsyncMock,
        side_effect=[None, None, None],
    ):
        capabilities = await detect_capabilities()

    assert capabilities["platforms"] == []
    assert capabilities["missing_prerequisites"] == []


async def test_detect_capabilities_does_not_require_global_appium_runtime() -> None:
    with patch(
        "agent_app.capabilities._get_tool_version",
        new_callable=AsyncMock,
        side_effect=["1.0.41", None, None],
    ):
        capabilities = await detect_capabilities()

    assert "appium" not in capabilities["missing_prerequisites"]


async def test_detect_capabilities_does_not_report_global_appium_runtime() -> None:
    with patch(
        "agent_app.capabilities._get_tool_version",
        new_callable=AsyncMock,
        side_effect=["1.0.41", None, None],
    ):
        capabilities = await detect_capabilities()

    assert "appium" not in capabilities["tools"]


async def test_detect_capabilities_checks_adapter_tools_by_command_name() -> None:
    with patch("agent_app.capabilities._get_tool_version", new_callable=AsyncMock, return_value=None) as get_version:
        await detect_capabilities()

    assert get_version.await_args_list[0].args[0] == "adb"
    assert get_version.await_args_list[2].args[0] == "ios"


async def test_capabilities_snapshot_refreshes_only_when_missing_or_forced() -> None:
    clear_capabilities_snapshot()

    first_snapshot = {"platforms": ["roku"], "tools": {"adb": "1.0.41"}, "missing_prerequisites": ["java"]}
    second_snapshot = {"platforms": ["roku"], "tools": {"adb": "1.0.42"}, "missing_prerequisites": []}
    with patch(
        "agent_app.capabilities.detect_capabilities",
        new_callable=AsyncMock,
        side_effect=[first_snapshot, second_snapshot],
    ) as detect:
        default_snapshot = {
            "platforms": [],
            "tools": {},
            "missing_prerequisites": [],
            "orchestration_contract_version": 2,
        }
        assert get_capabilities_snapshot() == default_snapshot
        assert await get_or_refresh_capabilities_snapshot() == {
            **first_snapshot,
            "orchestration_contract_version": 2,
        }
        assert await get_or_refresh_capabilities_snapshot() == {
            **first_snapshot,
            "orchestration_contract_version": 2,
        }
        assert await get_or_refresh_capabilities_snapshot(force=True) == {
            **second_snapshot,
            "orchestration_contract_version": 2,
        }

    assert detect.await_count == 2
    clear_capabilities_snapshot()


async def test_capabilities_refresh_loop_sleeps_first_when_refresh_immediately_false() -> None:
    clear_capabilities_snapshot()
    hit_sleep = asyncio.Event()
    _orig_sleep = asyncio.sleep

    async def fake_sleep(d: float) -> None:
        hit_sleep.set()
        await _orig_sleep(0.001)

    with (
        patch("agent_app.capabilities.refresh_capabilities_snapshot", new_callable=AsyncMock) as refresh,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        task = asyncio.create_task(capabilities_refresh_loop(interval_sec=1, refresh_immediately=False))
        await asyncio.wait_for(hit_sleep.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    refresh.assert_not_awaited()


async def test_capabilities_refresh_loop_exception_logged() -> None:
    clear_capabilities_snapshot()
    hit_sleep = asyncio.Event()
    _orig_sleep = asyncio.sleep

    async def fake_sleep(d: float) -> None:
        hit_sleep.set()
        await _orig_sleep(0.001)

    with (
        patch(
            "agent_app.capabilities.refresh_capabilities_snapshot",
            new_callable=AsyncMock,
            side_effect=RuntimeError("boom"),
        ) as refresh,
        patch("asyncio.sleep", side_effect=fake_sleep),
    ):
        task = asyncio.create_task(capabilities_refresh_loop(interval_sec=1, refresh_immediately=True))
        await asyncio.wait_for(hit_sleep.wait(), timeout=2.0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    refresh.assert_awaited()
