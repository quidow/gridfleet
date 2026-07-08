import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from app.appium_nodes.services import host_sweep
from app.appium_nodes.services.host_sweep import HostSweepLoop
from app.appium_nodes.services_container import AppiumNodeServices
from app.devices.services import fleet_capacity
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
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from app.sessions.appium_sweep import AppiumSweepLoop
from app.sessions.services_container import SessionServices
from tests.fakes import FakeSettingsReader, build_review_service
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
        settings=FakeSettingsReader({"general.heartbeat_interval_sec": 1}),
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
        sync=mock_sync,
        viability=viability_mock,
        settings=FakeSettingsReader({"grid.session_poll_interval_sec": 0.01}),
        session_factory=_Session,
        publisher=event_bus,
    )
    with pytest.raises(asyncio.CancelledError):
        await AppiumSweepLoop(services=services).run()

    mock_sync.sync.assert_awaited_once()
    viability_mock.check_due_devices.assert_awaited_once()


async def test_capacity_loop_covers_retry_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import background_loop

    monkeypatch.setattr(background_loop, "observe_background_loop", lambda *args, **kwargs: _Cycle())
    monkeypatch.setattr(
        fleet_capacity.FleetCapacityService,
        "collect_capacity_snapshot_once",
        AsyncMock(side_effect=[RuntimeError("boom"), None]),
    )
    monkeypatch.setattr(background_loop.asyncio, "sleep", AsyncMock(side_effect=[None, asyncio.CancelledError]))

    _fc_settings = FakeSettingsReader({})
    _fc_publisher = AsyncMock()
    _fc_maintenance = MaintenanceService(
        review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
    )
    _fc_crud = DeviceCrudService(settings=_fc_settings, identity=DeviceIdentityConflictService(), publisher=event_bus)
    loop = fleet_capacity.FleetCapacityLoop(
        services=DeviceServices(
            fleet_capacity=FleetCapacityService(),
            data_cleanup=DataCleanupService(publisher=_fc_publisher, settings=_fc_settings),
            property_refresh=PropertyRefreshService(discovery=Mock()),
            groups=DeviceGroupsService(publisher=_fc_publisher, crud=_fc_crud),
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
            session_factory=_Session,
            circuit_breaker=Mock(),
            health=AsyncMock(),
        )
    )

    with pytest.raises(asyncio.CancelledError):
        await loop.run()

    assert fleet_capacity.FleetCapacityService.collect_capacity_snapshot_once.await_count == 2
