import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium_process import AppiumProcessManager

pytestmark = pytest.mark.asyncio


async def test_advertise_ip_refresh_holds_start_lock_during_restart() -> None:
    """``refresh_grid_relay_advertise_ip`` must hold ``_start_lock`` while it
    calls ``_restart_grid_node_from_launch_spec``. Without the fix, a
    concurrent ``start()`` proceeds in parallel; with the fix, it blocks.
    """
    mgr = AppiumProcessManager()
    spec = type("Spec", (), {"manage_grid_node": True})()  # type: ignore[assignment]
    mgr._launch_specs[5555] = spec
    mgr._appium_procs[5555] = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    mgr._grid_advertise_ip = "10.0.0.1"

    inside_restart = asyncio.Event()
    proceed_restart = asyncio.Event()
    lock_was_held_during_restart = False

    async def fake_restart(*args: object, **kwargs: object) -> None:
        nonlocal lock_was_held_during_restart
        inside_restart.set()
        # Check if lock is currently locked. If _start_lock._locked is True, it's held.
        if hasattr(mgr._start_lock, "_locked"):
            lock_was_held_during_restart = mgr._start_lock._locked
        await proceed_restart.wait()

    with patch.object(
        mgr,
        "_restart_grid_node_from_launch_spec",
        new=AsyncMock(side_effect=fake_restart),
    ):
        refresher_task = asyncio.create_task(mgr.refresh_grid_relay_advertise_ip("10.0.0.2"))

        await inside_restart.wait()
        await asyncio.sleep(0.01)
        proceed_restart.set()
        await refresher_task

    assert lock_was_held_during_restart, "the refresh path is not holding _start_lock around the restart call"
