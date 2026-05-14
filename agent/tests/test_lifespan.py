from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.main import _stop_grid_node_supervisors_for_shutdown, app, appium_mgr, lifespan

pytestmark = pytest.mark.asyncio


class RecordingSupervisor:
    def __init__(self, name: str, order: list[str] | None = None) -> None:
        self.name = name
        self.order = order
        self.stop_called = False

    async def stop(self) -> None:
        self.stop_called = True
        if self.order is not None:
            self.order.append(self.name)


class FailingSupervisor(RecordingSupervisor):
    async def stop(self) -> None:
        self.stop_called = True
        raise RuntimeError("stop failed")


async def test_stop_grid_node_supervisors_for_shutdown_stops_all_and_clears_handles() -> None:
    first = RecordingSupervisor("first")
    second = RecordingSupervisor("second")
    manager = SimpleNamespace(_grid_supervisors={4723: first, 4724: second})

    await _stop_grid_node_supervisors_for_shutdown(manager, timeout_sec=1.0)

    assert first.stop_called is True
    assert second.stop_called is True
    assert manager._grid_supervisors == {}


async def test_stop_grid_node_supervisors_for_shutdown_keeps_failed_handles() -> None:
    failed = FailingSupervisor("failed")
    stopped = RecordingSupervisor("stopped")
    manager = SimpleNamespace(_grid_supervisors={4723: failed, 4724: stopped})

    await _stop_grid_node_supervisors_for_shutdown(manager, timeout_sec=1.0)

    assert failed.stop_called is True
    assert stopped.stop_called is True
    assert manager._grid_supervisors == {4723: failed}


async def test_lifespan_stops_grid_node_supervisors_before_appium_shutdown() -> None:
    stop_event = asyncio.Event()
    order: list[str] = []
    handle = RecordingSupervisor("grid-node", order)
    appium_mgr._grid_supervisors[4723] = handle

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    async def _record_shutdown() -> None:
        order.append("appium-shutdown")

    try:
        with (
            patch("agent_app.lifespan.refresh_capabilities_snapshot", new_callable=AsyncMock),
            patch("agent_app.lifespan.capabilities_refresh_loop", side_effect=_wait_forever),
            patch("agent_app.lifespan.registration_loop", side_effect=_wait_forever),
            patch("agent_app.appium.appium_mgr.shutdown", side_effect=_record_shutdown),
        ):
            async with lifespan(app):
                pass
    finally:
        stop_event.set()
        appium_mgr._grid_supervisors.clear()

    assert order == ["grid-node", "appium-shutdown"]
