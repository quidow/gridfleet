"""Reproducer for bug 10 (auto-restart side): ``_auto_restart_appium`` only
rechecked ``_intentional_stop_ports`` after the backoff sleep. A stop_pending
intent queued during the backoff window was ignored and the crashed appium
was resurrected against the operator's drain intent.

See ``.superpowers/specs/2026-05-20-agent-bug-audit.md`` (Bug 10).
"""

from __future__ import annotations

import asyncio
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.appium.process import AppiumLaunchSpec, AppiumProcessInfo, AppiumProcessManager

pytestmark = pytest.mark.asyncio


async def test_auto_restart_returns_when_stop_pending_queued_during_backoff() -> None:
    mgr = AppiumProcessManager()
    port = 5556

    fake_proc = AsyncMock()
    fake_proc.returncode = 1
    fake_proc.pid = 9001
    fake_proc.wait = AsyncMock(return_value=1)

    spec = AppiumLaunchSpec(
        connection_target="udid-test",
        port=port,
        plugins=None,
        extra_caps=None,
        session_override=False,
        device_type="real_device",
        ip_address="10.0.0.1",
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

    mgr._appium_procs[port] = cast("asyncio.subprocess.Process", fake_proc)
    mgr._launch_specs[port] = spec
    mgr._info[port] = AppiumProcessInfo(
        port=port,
        pid=9001,
        connection_target="udid-test",
        platform_id="android_mobile",
    )

    original_sleep = asyncio.sleep

    async def sleep_then_queue_stop_pending(_delay: float) -> None:
        """Operator queues a stop_pending lifecycle during the backoff window."""
        mgr._stop_pending_ports.add(port)
        await original_sleep(0)

    restart_from_launch_spec = AsyncMock(side_effect=RuntimeError("must not restart"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(asyncio, "sleep", sleep_then_queue_stop_pending)
        with patch.object(mgr, "_restart_from_launch_spec", new=restart_from_launch_spec):
            await asyncio.wait_for(mgr._auto_restart_appium(port, 1), timeout=2.0)

    assert restart_from_launch_spec.await_count == 0, (
        "_auto_restart_appium resurrected appium despite stop_pending queued during backoff"
    )
    assert port in mgr._stop_pending_ports
