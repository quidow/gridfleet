import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.appium_nodes.services import heartbeat as heartbeat
from app.appium_nodes.services import reconciler as appium_reconciler
from app.core.leader import keepalive, watcher
from app.core.leader.advisory import LeadershipLost
from app.devices.services import fleet_capacity as fleet_capacity
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.sessions import service_sync as session_sync
from app.sessions import service_viability as session_viability


class _Cycle:
    def cycle(self) -> "_Cycle":
        return self

    async def __aenter__(self) -> "_Cycle":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _Session:
    async def __aenter__(self) -> "_Session":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


async def test_appium_reconciler_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        appium_reconciler.settings_service, "get", lambda key: 0.01 if key.endswith("interval_sec") else 1
    )
    monkeypatch.setattr(appium_reconciler, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(appium_reconciler, "async_session", lambda: _Session())
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "_fetch_online_hosts", AsyncMock(return_value=[{"id": "bad"}]))
    monkeypatch.setattr(appium_reconciler, "_fetch_node_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_backoff_until", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "_reconcile_all", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "reconciler_convergence_enabled", lambda: True)
    monkeypatch.setattr(appium_reconciler, "_drive_convergence", AsyncMock())
    monkeypatch.setattr(appium_reconciler.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await appium_reconciler.appium_reconciler_loop()

    appium_reconciler._drive_convergence.assert_awaited_once()


async def test_heartbeat_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(heartbeat, "async_session", lambda: _Session())
    monkeypatch.setattr(heartbeat, "_check_hosts", AsyncMock())
    monkeypatch.setattr(heartbeat, "record_heartbeat_cycle", MagicMock())
    monkeypatch.setattr(heartbeat.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await heartbeat.heartbeat_loop()

    heartbeat._check_hosts.assert_awaited_once()
    heartbeat.record_heartbeat_cycle.assert_called_once()


async def test_session_viability_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_viability, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_viability, "async_session", lambda: _Session())
    monkeypatch.setattr(session_viability, "_check_due_devices", AsyncMock())
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await session_viability.session_viability_loop()

    session_viability._check_due_devices.assert_awaited_once()


async def test_session_sync_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_sync.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync, "async_session", lambda: _Session())
    monkeypatch.setattr(session_sync, "_sync_sessions", AsyncMock())
    monkeypatch.setattr(session_sync.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await session_sync.session_sync_loop()

    session_sync._sync_sessions.assert_awaited_once()


async def test_session_sync_loop_logs_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_sync.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync, "async_session", lambda: _Session())
    monkeypatch.setattr(session_sync, "_sync_sessions", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(session_sync.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await session_sync.session_sync_loop()


async def test_capacity_and_hardware_telemetry_loops_cover_retry_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fleet_capacity.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(fleet_capacity, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(fleet_capacity, "async_session", lambda: _Session())
    monkeypatch.setattr(
        fleet_capacity,
        "collect_capacity_snapshot_once",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(fleet_capacity.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    with pytest.raises(asyncio.CancelledError):
        await fleet_capacity.fleet_capacity_collector_loop()

    assert fleet_capacity.collect_capacity_snapshot_once.await_count == 2

    monkeypatch.setattr(hardware_telemetry.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(hardware_telemetry, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(hardware_telemetry, "async_session", lambda: _Session())
    monkeypatch.setattr(
        hardware_telemetry,
        "poll_hardware_telemetry_once",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(hardware_telemetry.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    with pytest.raises(asyncio.CancelledError):
        await hardware_telemetry.hardware_telemetry_loop()

    assert hardware_telemetry.poll_hardware_telemetry_once.await_count == 2


async def test_control_plane_loops_one_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(keepalive, "_setting", lambda key: 0.01)
    monkeypatch.setattr(keepalive, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(keepalive, "run_keepalive_once", AsyncMock())
    monkeypatch.setattr(keepalive.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await keepalive.control_plane_leader_keepalive_loop()

    keepalive.run_keepalive_once.assert_awaited_once()

    monkeypatch.setattr(watcher, "_setting", lambda key: 0.01)
    monkeypatch.setattr(watcher, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(watcher, "run_watcher_once", AsyncMock(side_effect=[RuntimeError("boom"), None]))
    monkeypatch.setattr(watcher.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    with pytest.raises(asyncio.CancelledError):
        await watcher.control_plane_leader_watcher_loop()

    assert watcher.run_watcher_once.await_count == 2


async def test_leadership_lost_loop_exit_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_exit(code: int) -> None:
        raise RuntimeError(f"exit {code}")

    monkeypatch.setattr(appium_reconciler.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(appium_reconciler, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(appium_reconciler, "async_session", lambda: _Session())
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock(side_effect=LeadershipLost("lost")))
    monkeypatch.setattr(appium_reconciler.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await appium_reconciler.appium_reconciler_loop()

    monkeypatch.setattr(heartbeat.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(heartbeat, "async_session", lambda: _Session())
    monkeypatch.setattr(heartbeat, "_check_hosts", AsyncMock(side_effect=LeadershipLost("lost")))
    monkeypatch.setattr(heartbeat, "record_heartbeat_cycle", MagicMock())
    monkeypatch.setattr(heartbeat.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await heartbeat.heartbeat_loop()

    monkeypatch.setattr(session_sync.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync, "async_session", lambda: _Session())
    monkeypatch.setattr(session_sync, "_sync_sessions", AsyncMock(side_effect=LeadershipLost("lost")))
    monkeypatch.setattr(session_sync.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await session_sync.session_sync_loop()
