"""Verify process-watch restart tasks are cancelled by stop()."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium_process import AppiumProcessManager

pytestmark = pytest.mark.asyncio


async def test_stop_cancels_restart_task_from_natural_crash() -> None:
    """A natural process exit followed by stop() must leave no restart task."""
    mgr = AppiumProcessManager()
    port = 5555

    proc_wait_future: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    fake_proc = AsyncMock()
    fake_proc.returncode = None
    fake_proc.pid = 1234
    fake_proc.send_signal = lambda *_a, **_k: None
    fake_proc.kill = lambda *_a, **_k: None

    async def fake_wait() -> int:
        return await proc_wait_future

    fake_proc.wait = fake_wait

    spec = type("Spec", (), {"manage_grid_node": False})()

    mgr._appium_procs[port] = fake_proc  # type: ignore[assignment]
    mgr._launch_specs[port] = spec  # type: ignore[assignment]
    mgr._info[port] = type(
        "Info",
        (),
        {
            "port": port,
            "pid": 1234,
            "connection_target": "udid-watch",
            "platform_id": "android_mobile",
        },
    )()  # type: ignore[assignment]

    watch_task = asyncio.create_task(mgr._watch_appium_process(port, fake_proc))  # type: ignore[arg-type]

    await asyncio.sleep(0)

    fake_proc.returncode = 1
    proc_wait_future.set_result(1)

    await asyncio.wait_for(watch_task, timeout=1.0)

    assert port in mgr._appium_restart_tasks, "Watch task should have created restart task"
    restart_task = mgr._appium_restart_tasks[port]
    assert not restart_task.done(), "Restart task should be running"

    with patch.object(mgr, "_stop_grid_node_process", new=AsyncMock()):
        await mgr.stop(port)

    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(restart_task, timeout=1.0)

    assert restart_task.cancelled() or restart_task.done(), "Restart task should be cancelled after stop()"
    assert port not in mgr._launch_specs, "stop() should have cleared launch spec"
    assert port not in mgr._appium_procs, "stop() should have cleared appium proc"
