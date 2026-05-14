import asyncio
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium.process import AppiumLaunchSpec, AppiumProcessManager

pytestmark = pytest.mark.asyncio


async def test_stop_holds_start_lock_during_process_teardown() -> None:
    """``stop()`` must hold ``_start_lock`` for its entire body — including
    the Grid Node service teardown that runs *before* the appium-proc/launch-spec
    dicts are popped. Without the fix, a concurrent ``start()`` for the
    same port can recreate ``_launch_specs[port]`` and discard the
    ``_intentional_stop_ports`` flag mid-stop. With the fix, any other
    coroutine attempting ``_start_lock.acquire()`` blocks until ``stop()``
    completes.
    """

    mgr = AppiumProcessManager()

    # Pre-load shared state so stop(5555) has work to do.
    fake_appium_proc = AsyncMock()
    fake_appium_proc.returncode = None
    fake_appium_proc.send_signal = lambda *_a, **_k: None
    fake_appium_proc.kill = lambda *_a, **_k: None
    fake_appium_proc.wait = AsyncMock(return_value=None)
    fake_appium_proc_typed = cast("asyncio.subprocess.Process", fake_appium_proc)
    mgr._appium_procs[5555] = fake_appium_proc_typed
    mgr._launch_specs[5555] = AppiumLaunchSpec(
        connection_target="udid-stop",
        port=5555,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=False,
        device_type="real_device",
        ip_address=None,
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
    )

    inside_stop = asyncio.Event()
    proceed_stop = asyncio.Event()
    starter_acquired = asyncio.Event()

    async def fake_grid_stop(*_args: object, **_kwargs: object) -> None:
        # Signal that stop() is past _intentional_stop_ports.add and has
        # entered _stop_grid_node_service — this is the moment a concurrent
        # start() would race the shared dicts.
        inside_stop.set()
        await proceed_stop.wait()

    async def stopper() -> None:
        with patch.object(
            AppiumProcessManager,
            "_stop_grid_node_service",
            new=AsyncMock(side_effect=fake_grid_stop),
        ):
            await mgr.stop(5555)

    async def starter() -> None:
        await inside_stop.wait()
        try:
            await asyncio.wait_for(mgr._start_lock.acquire(), timeout=0.05)
            starter_acquired.set()
            mgr._start_lock.release()
        except TimeoutError:
            # Expected when the fix is in place — stopper holds the lock
            # for longer than the starter's 50 ms wait window.
            pass

    starter_task = asyncio.create_task(starter())
    stopper_task = asyncio.create_task(stopper())

    await inside_stop.wait()
    await asyncio.sleep(0.1)
    proceed_stop.set()
    await asyncio.gather(starter_task, stopper_task)

    assert not starter_acquired.is_set(), (
        "starter acquired _start_lock while stopper was inside stop() — "
        "stop() is not holding _start_lock around its body, leaving the "
        "shared appium/launch-spec dicts and _intentional_stop_ports "
        "racey against concurrent start() for the same port"
    )
