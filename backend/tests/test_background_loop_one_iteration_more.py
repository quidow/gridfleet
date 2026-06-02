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
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.service import DeviceCrudService
from app.devices.services.test_data import TestDataService
from app.devices.services_container import DeviceServices
from app.hosts import service_hardware_telemetry as hardware_telemetry
from app.hosts.service import HostCrudService
from app.hosts.service_agent_logs import AgentLogsService
from app.hosts.service_diagnostics import HostDiagnosticsService
from app.hosts.service_hardware_telemetry import HardwareTelemetryLoop, HardwareTelemetryService
from app.hosts.service_host_events import HostEventsService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.services_container import HostServices
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from app.sessions import service_sync as session_sync
from app.sessions import service_viability as session_viability
from app.sessions.service_sync import SessionSyncLoop
from app.sessions.service_viability import SessionViabilityLoop
from app.sessions.services_container import SessionServices
from tests.fakes import FakeSettingsReader, build_diagnostics_export, build_review_service
from tests.helpers import test_event_bus as event_bus


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
    monkeypatch.setattr(appium_reconciler, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(appium_reconciler, "async_session", _Session)
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "_fetch_online_hosts", AsyncMock(return_value=[{"id": "bad"}]))
    monkeypatch.setattr(appium_reconciler, "_fetch_node_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_backoff_until", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "reconciler_convergence_enabled", lambda: True)
    monkeypatch.setattr(appium_reconciler.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    services = AppiumNodeServices(
        settings=FakeSettingsReader({}),
        reconciler=Mock(run_cycle=AsyncMock()),
        reconciler_agent=Mock(),
        node_health=Mock(),
        heartbeat=Mock(),
        session_factory=_Session,
    )

    with pytest.raises(asyncio.CancelledError):
        await AppiumReconcilerLoop(services=services).run()


async def test_heartbeat_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(heartbeat, "record_heartbeat_cycle", MagicMock())
    monkeypatch.setattr(heartbeat.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    heartbeat_mock = Mock(run_cycle=AsyncMock())
    services = AppiumNodeServices(
        settings=FakeSettingsReader({"general.heartbeat_interval_sec": "1"}),
        reconciler=Mock(run_cycle=AsyncMock()),
        reconciler_agent=Mock(),
        node_health=Mock(check_nodes=AsyncMock()),
        heartbeat=heartbeat_mock,
        session_factory=_Session,
    )

    with pytest.raises(asyncio.CancelledError):
        await HeartbeatLoop(services=services).run()

    heartbeat_mock.run_cycle.assert_awaited_once()
    heartbeat.record_heartbeat_cycle.assert_called_once()


async def test_session_viability_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_viability, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_viability.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    viability_mock = Mock()
    viability_mock.check_due_devices = AsyncMock()
    services = SessionServices(
        crud=Mock(),
        sync=Mock(),
        viability=viability_mock,
        settings=FakeSettingsReader({}),
        grid=Mock(),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(asyncio.CancelledError):
        await SessionViabilityLoop(services=services).run()

    viability_mock.check_due_devices.assert_awaited_once()


async def test_session_sync_loop_one_successful_iteration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    mock_sync = Mock()
    mock_sync.sync = AsyncMock()
    mock_sync.wait_for_wake = AsyncMock(side_effect=asyncio.CancelledError)  # exits the loop

    services = SessionServices(
        crud=Mock(),
        sync=mock_sync,
        viability=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 0.01}),
        grid=Mock(),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(asyncio.CancelledError):
        await SessionSyncLoop(services=services).run()

    mock_sync.sync.assert_awaited_once()


async def test_session_sync_loop_logs_unexpected_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    mock_sync = Mock()
    mock_sync.sync = AsyncMock(side_effect=RuntimeError("boom"))
    mock_sync.wait_for_wake = AsyncMock(side_effect=asyncio.CancelledError)  # exits after error

    services = SessionServices(
        crud=Mock(),
        sync=mock_sync,
        viability=Mock(),
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
        fleet_capacity.FleetCapacityService,
        "collect_capacity_snapshot_once",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(fleet_capacity.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    _fc_settings = FakeSettingsReader({})
    _fc_grid = Mock()
    _fc_publisher = AsyncMock()
    _fc_maintenance = MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    _fc_crud = DeviceCrudService(settings=_fc_settings, identity=DeviceIdentityConflictService(), publisher=event_bus)
    loop = fleet_capacity.FleetCapacityLoop(
        services=DeviceServices(
            diagnostics=build_diagnostics_export(),
            fleet_capacity=FleetCapacityService(grid=_fc_grid),
            data_cleanup=DataCleanupService(publisher=_fc_publisher, settings=_fc_settings),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=_fc_publisher, settings=_fc_settings, crud=_fc_crud),
            maintenance=_fc_maintenance,
            bulk=BulkOperationsService(
                publisher=_fc_publisher,
                settings=_fc_settings,
                circuit_breaker=Mock(),
                maintenance=_fc_maintenance,
                crud=_fc_crud,
                operator=OperatorNodeLifecycleService(
                    review=build_review_service(), settings=_fc_settings, publisher=event_bus
                ),
            ),
            presenter=DevicePresenterService(settings=_fc_settings),
            test_data=TestDataService(publisher=_fc_publisher),
            crud=_fc_crud,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=_fc_publisher,
                settings=_fc_settings,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            publisher=_fc_publisher,
            settings=_fc_settings,
            grid=_fc_grid,
            session_factory=_Session,
            circuit_breaker=Mock(),
            health=AsyncMock(),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    assert fleet_capacity.FleetCapacityService.collect_capacity_snapshot_once.await_count == 2

    monkeypatch.setattr(hardware_telemetry, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    poll_once_mock = AsyncMock(side_effect=[RuntimeError("boom"), None])
    monkeypatch.setattr(HardwareTelemetryService, "poll_once", poll_once_mock)
    monkeypatch.setattr(hardware_telemetry.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    _cb = Mock()
    loop = HardwareTelemetryLoop(
        services=HostServices(
            crud=HostCrudService(publisher=AsyncMock(), settings=FakeSettingsReader({})),
            hardware_telemetry=HardwareTelemetryService(
                publisher=AsyncMock(),
                settings=FakeSettingsReader({"general.hardware_telemetry_interval_sec": 0.01}),
                circuit_breaker=_cb,
            ),
            resource_telemetry=HostResourceTelemetryService(settings=FakeSettingsReader({}), circuit_breaker=_cb),
            diagnostics=HostDiagnosticsService(circuit_breaker=_cb),
            agent_logs=AgentLogsService(),
            host_events=HostEventsService(),
            publisher=AsyncMock(),
            settings=FakeSettingsReader({"general.hardware_telemetry_interval_sec": 0.01}),
            pool=Mock(),
            circuit_breaker=_cb,
            session_factory=_Session,
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    assert poll_once_mock.await_count == 2


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
    monkeypatch.setattr(appium_reconciler.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await AppiumReconcilerLoop(
            services=AppiumNodeServices(
                settings=FakeSettingsReader({}),
                reconciler=Mock(run_cycle=AsyncMock(side_effect=LeadershipLost("lost"))),
                reconciler_agent=Mock(),
                node_health=Mock(check_nodes=AsyncMock()),
                heartbeat=Mock(run_cycle=AsyncMock()),
                session_factory=_Session,
            )
        ).run()

    monkeypatch.setattr(heartbeat, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(heartbeat, "record_heartbeat_cycle", MagicMock())
    monkeypatch.setattr(heartbeat.os, "_exit", fake_exit)
    with pytest.raises(RuntimeError, match="exit 70"):
        await HeartbeatLoop(
            services=AppiumNodeServices(
                settings=FakeSettingsReader({"general.heartbeat_interval_sec": "1"}),
                reconciler=Mock(run_cycle=AsyncMock()),
                reconciler_agent=Mock(),
                node_health=Mock(check_nodes=AsyncMock()),
                heartbeat=Mock(run_cycle=AsyncMock(side_effect=LeadershipLost("lost"))),
                session_factory=_Session,
            )
        ).run()

    monkeypatch.setattr(session_sync, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(session_sync.os, "_exit", fake_exit)
    mock_sync = Mock()
    mock_sync.sync = AsyncMock(side_effect=LeadershipLost("lost"))
    mock_sync.wait_for_wake = AsyncMock()
    services = SessionServices(
        crud=Mock(),
        sync=mock_sync,
        viability=Mock(),
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 0.01}),
        grid=Mock(),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(RuntimeError, match="exit 70"):
        await SessionSyncLoop(services=services).run()
