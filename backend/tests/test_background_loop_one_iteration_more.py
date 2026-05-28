import asyncio
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from app.appium_nodes.services import heartbeat as heartbeat
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.heartbeat import HeartbeatLoop
from app.appium_nodes.services.reconciler import AppiumReconcilerLoop
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.leader import keepalive, watcher
from app.core.leader.advisory import LeadershipLost
from app.devices.services import fleet_capacity as fleet_capacity
from app.devices.services_container import DeviceServices
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.hosts.service_hardware_telemetry import HardwareTelemetryLoop
from app.hosts.services_container import HostServices
from app.sessions import service_sync as session_sync
from app.sessions import service_viability as session_viability
from app.sessions.service_sync import SessionSyncLoop
from app.sessions.service_viability import SessionViabilityLoop
from app.sessions.services_container import SessionServices
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


@pytest.fixture(autouse=True)
def _reset_session_sync_doorbell() -> None:
    """Force a fresh doorbell Event on each test's event loop.

    ``session_sync_loop`` lazy-binds ``_doorbell`` on first call. Across
    pytest-xdist workers this can re-use an Event from a dead loop and
    raise ``RuntimeError: bound to a different event loop``.
    """
    session_sync._doorbell = None


class _Cycle:
    def cycle(self) -> "_Cycle":
        return self

    async def __aenter__(self) -> "_Cycle":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def _cancel_after_closing(awaitable: object, *_args: object, **_kwargs: object) -> None:
    # Drop the doorbell.wait() coroutine that callers construct before invoking
    # asyncio.wait_for, otherwise the mocked CancelledError leaves it unawaited.
    if hasattr(awaitable, "close"):
        awaitable.close()
    raise asyncio.CancelledError


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_appium_reconciler_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appium_reconciler, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(appium_reconciler, "async_session", _Session)
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "_fetch_online_hosts", AsyncMock(return_value=[{"id": "bad"}]))
    monkeypatch.setattr(appium_reconciler, "_fetch_node_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_backoff_until", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "_reconcile_all", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "reconciler_convergence_enabled", lambda: True)
    monkeypatch.setattr(appium_reconciler, "_drive_convergence", AsyncMock())
    monkeypatch.setattr(appium_reconciler.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    services = AppiumNodeServices(
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        publisher=Mock(),
        grid=Mock(),
        session_factory=_Session,
    )

    with pytest.raises(asyncio.CancelledError):
        await AppiumReconcilerLoop(services=services).run()

    appium_reconciler._drive_convergence.assert_awaited_once()


async def test_heartbeat_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(heartbeat.HeartbeatLoop, "_check_hosts", AsyncMock())
    monkeypatch.setattr(heartbeat, "record_heartbeat_cycle", MagicMock())
    monkeypatch.setattr(heartbeat.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    services = AppiumNodeServices(
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        publisher=Mock(),
        grid=Mock(),
        session_factory=_Session,
    )

    with pytest.raises(asyncio.CancelledError):
        await HeartbeatLoop(services=services).run()

    heartbeat.HeartbeatLoop._check_hosts.assert_awaited_once()
    heartbeat.record_heartbeat_cycle.assert_called_once()


async def test_session_viability_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_viability, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_viability, "_check_due_devices", AsyncMock())
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    services = SessionServices(
        crud=Mock(), settings=FakeSettingsReader({}), grid=Mock(), session_factory=_Session, publisher=event_bus
    )
    with pytest.raises(asyncio.CancelledError):
        await SessionViabilityLoop(services=services).run()

    session_viability._check_due_devices.assert_awaited_once()


async def test_session_sync_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync, "_sync_sessions", AsyncMock())
    monkeypatch.setattr(session_sync.asyncio, "wait_for", _cancel_after_closing)

    services = SessionServices(
        crud=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 0.01}),
        grid=Mock(),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(asyncio.CancelledError):
        await SessionSyncLoop(services=services).run()

    session_sync._sync_sessions.assert_awaited_once()


async def test_session_sync_loop_logs_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync, "_sync_sessions", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(session_sync.asyncio, "wait_for", _cancel_after_closing)

    services = SessionServices(
        crud=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 0.01}),
        grid=Mock(),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(asyncio.CancelledError):
        await SessionSyncLoop(services=services).run()


async def test_capacity_and_hardware_telemetry_loops_cover_retry_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_capacity, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(
        fleet_capacity,
        "collect_capacity_snapshot_once",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(fleet_capacity.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    loop = fleet_capacity.FleetCapacityLoop(
        services=DeviceServices(
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            grid=Mock(),
            session_factory=_Session,
            circuit_breaker=Mock(),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    assert fleet_capacity.collect_capacity_snapshot_once.await_count == 2

    monkeypatch.setattr(hardware_telemetry, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(
        hardware_telemetry,
        "poll_hardware_telemetry_once",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(hardware_telemetry.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    loop = HardwareTelemetryLoop(
        services=HostServices(
            publisher=AsyncMock(),
            settings=FakeSettingsReader({"general.hardware_telemetry_interval_sec": 0.01}),
            pool=Mock(),
            circuit_breaker=Mock(),
            session_factory=_Session,
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    assert hardware_telemetry.poll_hardware_telemetry_once.await_count == 2


async def test_control_plane_loops_one_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = FakeSettingsReader({"general.leader_keepalive_interval_sec": 0.01})
    monkeypatch.setattr(keepalive, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(keepalive, "run_keepalive_once", AsyncMock())
    monkeypatch.setattr(keepalive.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await keepalive.LeaderKeepaliveLoop(settings=settings).run()

    keepalive.run_keepalive_once.assert_awaited_once()

    monkeypatch.setattr(watcher, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(watcher, "run_watcher_once", AsyncMock(side_effect=[RuntimeError("boom"), None]))
    monkeypatch.setattr(watcher.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    with pytest.raises(asyncio.CancelledError):
        await watcher.LeaderWatcherLoop(settings=settings, leader=Mock(), engine=Mock()).run()

    assert watcher.run_watcher_once.await_count == 2


async def test_leadership_lost_loop_exit_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_exit(code: int) -> None:
        raise RuntimeError(f"exit {code}")

    monkeypatch.setattr(appium_reconciler, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(appium_reconciler, "async_session", _Session)
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock(side_effect=LeadershipLost("lost")))
    monkeypatch.setattr(appium_reconciler.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await AppiumReconcilerLoop(
            services=AppiumNodeServices(
                settings=FakeSettingsReader({}),
                pool=Mock(),
                circuit_breaker=Mock(),
                publisher=Mock(),
                grid=Mock(),
                session_factory=_Session,
            )
        ).run()

    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(heartbeat.HeartbeatLoop, "_check_hosts", AsyncMock(side_effect=LeadershipLost("lost")))
    monkeypatch.setattr(heartbeat, "record_heartbeat_cycle", MagicMock())
    monkeypatch.setattr(heartbeat.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await HeartbeatLoop(
            services=AppiumNodeServices(
                settings=FakeSettingsReader({}),
                pool=Mock(),
                circuit_breaker=Mock(),
                publisher=Mock(),
                grid=Mock(),
                session_factory=_Session,
            )
        ).run()

    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync, "_sync_sessions", AsyncMock(side_effect=LeadershipLost("lost")))
    monkeypatch.setattr(session_sync.os, "_exit", fake_exit)
    services = SessionServices(
        crud=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 0.01}),
        grid=Mock(),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(RuntimeError, match="exit 70"):
        await SessionSyncLoop(services=services).run()
