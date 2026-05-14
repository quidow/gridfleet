import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.host.capabilities import (
    _resolve_tool_command,
    _run_cmd,
    _snapshot_is_stale,
    capabilities_refresh_loop,
    clear_capabilities_snapshot,
    refresh_capabilities_snapshot,
)


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self) -> asyncio.Future[tuple[bytes, bytes]]:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[tuple[bytes, bytes]] = loop.create_future()
        future.set_result((self._stdout, self._stderr))
        return future


def test_resolve_tool_command_and_snapshot_staleness() -> None:
    assert _resolve_tool_command("adb", None) == "adb"
    assert _resolve_tool_command("go_ios", "ios") == "ios"
    assert _resolve_tool_command("xcodebuild", "custom-xcodebuild") == "custom-xcodebuild"

    clear_capabilities_snapshot()
    assert _snapshot_is_stale() is True


async def test_run_cmd_refresh_and_loop_cover_remaining_paths() -> None:
    with patch(
        "agent_app.host.capabilities.asyncio.create_subprocess_exec",
        return_value=_FakeProc(0, stdout=b"ok\n"),
    ):
        assert await _run_cmd("echo", "ok") == "ok"

    with patch(
        "agent_app.host.capabilities.asyncio.create_subprocess_exec",
        return_value=_FakeProc(1, stderr=b"boom"),
    ):
        assert await _run_cmd("echo", "ok") is None

    with patch("agent_app.host.capabilities.asyncio.create_subprocess_exec", side_effect=FileNotFoundError):
        assert await _run_cmd("echo", "ok") is None

    snapshot = {"platforms": ["roku"], "tools": {"appium": "3.0.0"}, "missing_prerequisites": []}
    with patch("agent_app.host.capabilities.detect_capabilities", new_callable=AsyncMock, return_value=snapshot):
        assert await refresh_capabilities_snapshot() == snapshot

    sleep_calls = 0

    async def _sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        raise asyncio.CancelledError

    with (
        patch(
            "agent_app.host.capabilities.refresh_capabilities_snapshot",
            new_callable=AsyncMock,
            side_effect=RuntimeError,
        ),
        patch("agent_app.host.capabilities.asyncio.sleep", side_effect=_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await capabilities_refresh_loop(interval_sec=1, refresh_immediately=True)
