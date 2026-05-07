"""Verify auto-restart exits cleanly when stop() removes launch spec during sleep."""

import asyncio
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium_process import AppiumLaunchSpec, AppiumProcessInfo, AppiumProcessManager

pytestmark = pytest.mark.asyncio


async def test_auto_restart_exits_when_launch_spec_removed() -> None:
    """A removed launch spec means stop() won and restart must not run."""
    mgr = AppiumProcessManager()
    port = 5555

    fake_proc = AsyncMock()
    fake_proc.returncode = 1
    fake_proc.pid = 1234
    fake_proc.wait = AsyncMock(return_value=1)

    spec = AppiumLaunchSpec(
        connection_target="udid-test",
        port=port,
        plugins=None,
        extra_caps=None,
        stereotype_caps=None,
        session_override=False,
        device_type="real_device",
        ip_address="10.0.0.1",
        manage_grid_node=False,
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        appium_platform_name=None,
        workaround_env=None,
        insecure_features=[],
        grid_slots=["native"],
        lifecycle_actions=[],
        connection_behavior={},
        headless=False,
    )

    fake_proc_typed = cast("asyncio.subprocess.Process", fake_proc)

    mgr._appium_procs[port] = fake_proc_typed
    mgr._launch_specs[port] = spec
    mgr._info[port] = AppiumProcessInfo(
        port=port,
        pid=1234,
        connection_target="udid-test",
        platform_id="android_mobile",
    )

    original_sleep = asyncio.sleep

    async def sleep_then_clear(_delay: float) -> None:
        """Simulate stop() running during the sleep window."""
        mgr._launch_specs.pop(port, None)
        mgr._appium_procs.pop(port, None)
        mgr._info.pop(port, None)
        await original_sleep(0)

    restart_from_launch_spec = AsyncMock(side_effect=RuntimeError("launch spec missing"))
    restart_task = asyncio.create_task(mgr._auto_restart_appium(port, 1))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "sleep", sleep_then_clear)
        with patch.object(mgr, "_restart_from_launch_spec", new=restart_from_launch_spec):
            try:
                await asyncio.wait_for(restart_task, timeout=2.0)
            except TimeoutError:
                restart_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await restart_task
                pytest.fail("_auto_restart_appium did not exit after launch spec was removed")

    assert restart_from_launch_spec.await_count == 0, (
        "_auto_restart_appium tried to restart after stop() removed the launch spec"
    )
