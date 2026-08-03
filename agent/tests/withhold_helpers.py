"""Shared fixtures for the first-restart withhold regression tests.

A manager whose Appium on ``PORT`` has just exited with its bookkeeping
intact is the common starting point for every withhold scenario: the
takeover race, the wall-clock backstop, and the deferred-start handoff.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from agent_app.appium.process import (
    AppiumInvocation,
    AppiumLaunchSpec,
    AppiumProcessInfo,
    AppiumProcessManager,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

PORT = 4723
TARGET = "device-001"
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
CRASHED_PID = 9001
RESPAWNED_PID = 9002


class FakeProcess:
    """A child that stays alive until the test says otherwise."""

    def __init__(self, pid: int, returncode: int | None = None) -> None:
        self.pid = pid
        self.returncode = returncode
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        if self.returncode is not None:
            return self.returncode
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0


async def _never_connects(_self: object, _port: int) -> bool:
    return False


@pytest.fixture
def stub_port_probe() -> Iterator[None]:
    with (
        patch("agent_app.appium.process.AppiumProcessManager._can_connect_to_appium", new=_never_connects),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
    ):
        yield


def manager_with_crashed_node() -> AppiumProcessManager:
    """A manager whose Appium on PORT has just exited, bookkeeping intact."""
    mgr = AppiumProcessManager()
    mgr._appium_procs[PORT] = cast("asyncio.subprocess.Process", FakeProcess(CRASHED_PID, returncode=1))
    mgr._launch_specs[PORT] = AppiumLaunchSpec(
        connection_target=TARGET,
        port=PORT,
        extra_caps=None,
        session_override=False,
        device_type="real_device",
        ip_address="10.0.0.1",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        appium_platform_name=None,
        appium_env=None,
        insecure_features=[],
        lifecycle_actions=[],
        connection_behavior={},
    )
    mgr._info[PORT] = AppiumProcessInfo(
        port=PORT,
        pid=CRASHED_PID,
        connection_target=TARGET,
        platform_id="android_mobile",
    )
    return mgr


async def restart_task_in_backoff(mgr: AppiumProcessManager) -> asyncio.Task[None]:
    """Drive the real auto-restart task to its first-attempt backoff sleep."""
    task = asyncio.create_task(mgr._auto_restart_appium(PORT, 1))
    mgr._register_port_task(mgr._appium_restart_tasks, PORT, task)
    await asyncio.sleep(0)
    assert set(mgr._withheld_restart_by_port) == {PORT}, "the first attempt did not enter withholding"
    assert not task.done()
    return task


async def settle(times: int = 5) -> None:
    for _ in range(times):
        await asyncio.sleep(0)


def kinds(snapshot: dict[str, Any]) -> list[str]:
    return [event["kind"] for event in snapshot["recent_restart_events"]]
