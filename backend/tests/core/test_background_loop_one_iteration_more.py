import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.appium_nodes.services import host_sweep
from app.appium_nodes.services.host_sweep import HostSweepLoop
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.janitor import JANITOR_BASE_INTERVAL_SEC, JanitorLoop, JanitorStage
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


class _CountingSessionFactory:
    """Hands out a fresh ``_Session`` per call and counts how many it gave out."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> _Session:
        self.calls += 1
        return _Session()


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


async def test_janitor_loop_one_iteration_opens_a_session_per_due_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(background_loop.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    factory = _CountingSessionFactory()
    stage_one = AsyncMock()
    stage_two = AsyncMock()
    loop = JanitorLoop(
        session_factory=factory,
        stages=(
            JanitorStage("one", JANITOR_BASE_INTERVAL_SEC, stage_one),
            JanitorStage("two", JANITOR_BASE_INTERVAL_SEC, stage_two),
        ),
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    stage_one.assert_awaited_once()
    stage_two.assert_awaited_once()
    # One checkout for BackgroundLoop.run's own tick session (unused, no
    # statement issued) plus one fresh session per due stage.
    assert factory.calls == 3
