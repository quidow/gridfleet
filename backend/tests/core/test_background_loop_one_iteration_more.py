import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.appium_nodes.services import host_sweep
from app.appium_nodes.services.host_sweep import HostSweepLoop
from app.appium_nodes.services_container import AppiumNodeServices
from app.sessions.appium_sweep import AppiumSweepLoop
from app.sessions.services_container import SessionServices
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


class _Cycle:
    def cycle(self) -> _Cycle:
        return self

    async def __aenter__(self) -> _Cycle:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_host_sweep_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    run_once = AsyncMock()
    monkeypatch.setattr(host_sweep, "run_host_sweep_once", run_once)
    monkeypatch.setattr(background_loop.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    services = AppiumNodeServices(
        settings=FakeSettingsReader({}),
        reconciler=Mock(reconcile_host=AsyncMock()),
        reconciler_agent=Mock(),
        node_health=Mock(),
        heartbeat=Mock(),
        session_factory=_Session,
    )

    with pytest.raises(asyncio.CancelledError):
        await HostSweepLoop(services=services).run()

    run_once.assert_awaited_once()


async def test_appium_sweep_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    mock_sync = Mock()
    mock_sync.sync = AsyncMock()
    mock_sync.wait_for_wake = AsyncMock(side_effect=asyncio.CancelledError)
    viability_mock = Mock()
    viability_mock.check_due_devices = AsyncMock()
    services = SessionServices(
        crud=Mock(),
        kill=Mock(),
        sync=mock_sync,
        viability=viability_mock,
        settings=FakeSettingsReader({}),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(asyncio.CancelledError):
        await AppiumSweepLoop(services=services).run()

    mock_sync.sync.assert_awaited_once()
    viability_mock.check_due_devices.assert_awaited_once()
