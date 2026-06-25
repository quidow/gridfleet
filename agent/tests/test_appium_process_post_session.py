from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from agent_app.appium.process import AppiumLaunchSpec, AppiumProcessManager
from agent_app.pack.adapter_registry import AdapterRegistry
from agent_app.pack.adapter_types import SessionOutcome, SessionSpec

if TYPE_CHECKING:
    import asyncio

pytestmark = pytest.mark.asyncio


class _PostSessionRecorder:
    """Minimal adapter that records ``post_session`` dispatches."""

    def __init__(self, pack_id: str, pack_release: str) -> None:
        self.pack_id = pack_id
        self.pack_release = pack_release
        self.calls: list[tuple[SessionSpec, SessionOutcome]] = []

    async def post_session(self, spec: SessionSpec, outcome: SessionOutcome) -> None:
        self.calls.append((spec, outcome))


def _preload(mgr: AppiumProcessManager, port: int, pack_id: str) -> None:
    fake_proc = AsyncMock()
    fake_proc.returncode = None
    fake_proc.send_signal = lambda *_a, **_k: None
    fake_proc.kill = lambda *_a, **_k: None
    mgr._appium_procs[port] = cast("asyncio.subprocess.Process", fake_proc)
    mgr._launch_specs[port] = AppiumLaunchSpec(
        connection_target="udid-stop",
        port=port,
        plugins=None,
        extra_caps=None,
        session_override=False,
        device_type="real_device",
        ip_address=None,
        pack_id=pack_id,
        platform_id="android_mobile",
    )


async def test_stop_dispatches_post_session_to_adapter() -> None:
    """``stop()`` is the symmetric teardown for the start-path ``pre_session``
    call: stopping a node must fire the adapter ``post_session`` cleanup hook."""
    mgr = AppiumProcessManager()
    adapter = _PostSessionRecorder("appium-uiautomator2", "1.0.0")
    registry = AdapterRegistry()
    registry.set(adapter.pack_id, adapter.pack_release, cast("object", adapter))  # type: ignore[arg-type]
    mgr.set_adapter_registry(registry)
    _preload(mgr, 5556, adapter.pack_id)

    await mgr.stop(5556)

    assert len(adapter.calls) == 1
    spec, outcome = adapter.calls[0]
    assert spec.pack_id == adapter.pack_id
    assert spec.device_identity_value == "udid-stop"
    assert spec.platform_id == "android_mobile"
    assert outcome.ok is True


async def test_stop_without_adapter_registry_is_a_noop() -> None:
    """No adapter registry → no dispatch, and teardown still completes."""
    mgr = AppiumProcessManager()
    _preload(mgr, 5557, "appium-uiautomator2")

    await mgr.stop(5557)  # must not raise

    assert 5557 not in mgr._launch_specs
