"""A child that died instantly is reported as an exit, not a 30s timeout, and
each spawn writes to its own log file so racing processes are distinguishable."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium.exceptions import AppiumExitedError, StartupTimeoutError
from agent_app.appium.log_files import appium_log_dir, port_log_paths
from agent_app.appium.process import AppiumInvocation, AppiumProcessManager

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
PORT = 4723


class _FakeProcess:
    def __init__(self, pid: int = 999, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def start_stubs() -> Iterator[None]:
    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=_never_connects),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
    ):
        yield


async def _never_connects(_self: object, _port: int) -> bool:
    return False


async def test_a_child_that_died_reports_its_exit_code_not_a_timeout(start_stubs: None) -> None:
    mgr = AppiumProcessManager()
    dead = _FakeProcess(returncode=1)
    with (
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", new=AsyncMock(return_value=dead)),
        patch.object(mgr, "_wait_for_readiness", new=AsyncMock(return_value=False)),
        pytest.raises(AppiumExitedError) as excinfo,
    ):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)
    message = str(excinfo.value)
    assert "exited with code 1" in message
    assert "30s" not in message


async def test_a_live_child_that_never_answers_is_still_a_timeout(start_stubs: None) -> None:
    mgr = AppiumProcessManager()
    with (
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess(returncode=None)),
        ),
        patch.object(mgr, "_wait_for_readiness", new=AsyncMock(return_value=False)),
        pytest.raises(StartupTimeoutError) as excinfo,
    ):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)
    assert "did not become ready within" in str(excinfo.value)


async def test_each_spawn_writes_its_own_log_file(start_stubs: None) -> None:
    mgr = AppiumProcessManager()
    with (
        patch(
            "agent_app.appium.process.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_FakeProcess()),
        ),
        patch.object(mgr, "_wait_for_readiness", new=AsyncMock(return_value=True)),
    ):
        await mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS)
        paths = port_log_paths(PORT)

    assert paths, "the spawn wrote no log file"
    assert paths[0].parent == appium_log_dir()
    assert paths[0].name.startswith(f"appium-{PORT}-")
    assert paths[0].name != f"appium-{PORT}.log"
    await mgr.shutdown()
