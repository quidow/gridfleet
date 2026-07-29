"""D1: cancelling a port's auto-restart task must never orphan a spawned child.

``start()`` cancels the restart task for the port it is about to take. Every
Appium spawn happens inside ``_start_appium_server``, which runs under
``_start_lock`` — so the cancel is only safe while that lock is held.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from agent_app.appium.exceptions import AlreadyRunningError
from agent_app.appium.process import AppiumInvocation, AppiumProcessInfo, AppiumProcessManager

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.asyncio

_STUB_INVOCATION = AppiumInvocation(binary="/usr/local/bin/appium")
PACK_START_KWARGS = {"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}
PORT = 4723


class _FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode if self.returncode is not None else 0


@pytest.fixture
def stub_port_probe() -> Iterator[None]:
    with (
        patch(
            "agent_app.appium.process.AppiumProcessManager._can_connect_to_appium",
            new=_never_connects,
        ),
        patch("agent_app.appium.process.AppiumProcessManager._is_appium_port_bindable", return_value=True),
    ):
        yield


async def _never_connects(_self: object, _port: int) -> bool:
    return False


async def test_start_never_orphans_a_child_spawned_by_the_restart_task_it_cancels(
    stub_port_probe: None,
) -> None:
    """The restart task is mid-readiness when a second start() arrives. Exactly
    one child may exist, and it must be the registered one."""
    mgr = AppiumProcessManager()
    spawned: list[_FakeProcess] = []
    readiness_entered = asyncio.Event()
    release_readiness = asyncio.Event()

    async def fake_spawn(*_args: object, **_kwargs: object) -> _FakeProcess:
        proc = _FakeProcess(pid=1000 + len(spawned))
        spawned.append(proc)
        return proc

    async def fake_readiness(_port: int, _proc: object) -> bool:
        readiness_entered.set()
        await release_readiness.wait()
        return True

    with (
        patch("agent_app.appium.process.resolve_appium_invocation_for_pack", return_value=_STUB_INVOCATION),
        patch("agent_app.appium.process.build_env", return_value={"PATH": "/usr/bin"}),
        patch.object(mgr, "_wait_for_readiness", new=fake_readiness),
        patch("agent_app.appium.process.asyncio.create_subprocess_exec", side_effect=fake_spawn),
    ):
        restart = asyncio.create_task(mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS))
        mgr._appium_restart_tasks[PORT] = restart  # the auto-restart task for this port
        await asyncio.wait_for(readiness_entered.wait(), timeout=2)

        node_loop_start = asyncio.create_task(mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS))
        await asyncio.sleep(0)  # let the second start reach the lock
        release_readiness.set()
        outcomes: list[Any] = await asyncio.gather(restart, node_loop_start, return_exceptions=True)

    assert len(spawned) == 1, (
        f"start() cancelled a restart task mid-spawn and leaked its child ({len(spawned)} spawned)"
    )
    assert mgr._appium_procs.get(PORT) is spawned[0]
    assert any(isinstance(outcome, AppiumProcessInfo) for outcome in outcomes)
    assert any(isinstance(outcome, AlreadyRunningError) for outcome in outcomes)
    await mgr.shutdown()


async def test_start_cancels_the_restart_task_only_while_holding_the_start_lock() -> None:
    """The pending restart stays untouched until this start owns the spawn lock."""
    mgr = AppiumProcessManager()
    never_finishes = asyncio.create_task(asyncio.Event().wait())
    mgr._appium_restart_tasks[PORT] = never_finishes  # type: ignore[assignment]

    await mgr._start_lock.acquire()
    starter = asyncio.create_task(mgr.start(connection_target="device-001", port=PORT, **PACK_START_KWARGS))
    try:
        await asyncio.sleep(0.05)
        assert not never_finishes.cancelled(), "restart task cancelled before the start lock was taken"
    finally:
        mgr._start_lock.release()
    with pytest.raises(Exception):  # noqa: B017 - the start fails on the stubbed-out runtime; the cancel is the subject
        await asyncio.wait_for(starter, timeout=2)
    await asyncio.sleep(0)
    assert never_finishes.cancelled() or never_finishes.done()
    never_finishes.cancel()
