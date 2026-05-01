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
    mgr._launch_specs[5555] = type("Spec", (), {"manage_grid_node": True})()  # type: ignore[assignment]
    mgr._appium_procs[5555] = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    mgr._grid_advertise_ip = "10.0.0.1"

    inside_restart = asyncio.Event()
    proceed_restart = asyncio.Event()
    starter_acquired = asyncio.Event()

    async def fake_restart(*_args: object, **_kwargs: object) -> None:
        inside_restart.set()
        await proceed_restart.wait()

    async def refresher() -> None:
        with patch.object(
            AppiumProcessManager,
            "_restart_grid_node_from_launch_spec",
            new=AsyncMock(side_effect=fake_restart),
        ):
            await mgr.refresh_grid_relay_advertise_ip("10.0.0.2")

    async def starter() -> None:
        await inside_restart.wait()
        try:
            await asyncio.wait_for(mgr._start_lock.acquire(), timeout=0.05)
            starter_acquired.set()
            mgr._start_lock.release()
        except TimeoutError:
            pass

    starter_task = asyncio.create_task(starter())
    refresher_task = asyncio.create_task(refresher())
    await inside_restart.wait()
    await asyncio.sleep(0.1)
    proceed_restart.set()
    await starter_task
    await refresher_task

    assert not starter_acquired.is_set(), (
        "starter acquired _start_lock while refresher was inside "
        "_restart_grid_node_from_launch_spec — the refresh path is not "
        "holding _start_lock around the restart call"
    )
