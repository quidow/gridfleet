import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.devices.services.bulk import BulkOperationsService
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.inventory_export import InventoryExportService
from app.devices.services.lifecycle_incidents import LifecycleIncidentService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.operator_node_lifecycle import OperatorNodeLifecycleService
from app.devices.services.portability_export import PortabilityExportService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshLoop, PropertyRefreshService
from app.devices.services.service import DeviceCrudService
from app.devices.services.test_data import TestDataService
from app.devices.services.verification import VerificationService
from app.devices.services_container import DeviceServices
from app.hosts.models import Host, HostStatus, OSType
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device_record


async def test_property_refresh_only_visits_online_hosts_and_non_offline_devices(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    online_host = Host(
        hostname="online-host",
        ip="10.0.0.10",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    offline_host = Host(
        hostname="offline-host",
        ip="10.0.0.11",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add_all([online_host, offline_host])
    await db_session.flush()

    online_device = await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="refresh-001",
        connection_target="refresh-001",
        name="Refresh One",
        operational_state="available",
    )
    offline_device = await create_device_record(
        db_session,
        host_id=online_host.id,
        identity_value="refresh-002",
        connection_target="refresh-002",
        name="Refresh Two",
        operational_state="offline",
    )
    offline_host_device = await create_device_record(
        db_session,
        host_id=offline_host.id,
        identity_value="refresh-003",
        connection_target="refresh-003",
        name="Refresh Three",
        operational_state="available",
    )

    fetch_props = AsyncMock(return_value=None)

    class _DiscoveryDouble:
        fetch_pack_device_properties = fetch_props
        apply_pack_device_properties = AsyncMock()

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await svc.refresh_all_properties(db)

    refreshed_identity_values = [await_call.args[1].identity_value for await_call in fetch_props.await_args_list]
    assert online_device.identity_value in refreshed_identity_values
    assert offline_device.identity_value not in refreshed_identity_values
    assert offline_host_device.identity_value not in refreshed_identity_values


async def test_property_refresh_continues_after_device_failure(
    db_session: AsyncSession,
    setup_database: AsyncEngine,
) -> None:
    host = Host(
        hostname="online-host",
        ip="10.0.0.12",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.flush()

    first = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="refresh-a",
        connection_target="refresh-a",
        name="Refresh A",
        operational_state="available",
    )
    second = await create_device_record(
        db_session,
        host_id=host.id,
        identity_value="refresh-b",
        connection_target="refresh-b",
        name="Refresh B",
        operational_state="available",
    )

    fetch_props = AsyncMock(side_effect=[RuntimeError("boom"), None])

    class _DiscoveryDouble:
        fetch_pack_device_properties = fetch_props
        apply_pack_device_properties = AsyncMock()

    svc = PropertyRefreshService(discovery=_DiscoveryDouble())
    session_factory = async_sessionmaker(setup_database, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        await svc.refresh_all_properties(db)

    refreshed_identity_values = sorted(await_call.args[1].identity_value for await_call in fetch_props.await_args_list)
    assert refreshed_identity_values == sorted([first.identity_value, second.identity_value])


async def test_property_refresh_loop_logs_cycle_failure_and_sleeps() -> None:
    class _Observation:
        @asynccontextmanager
        async def cycle(self) -> AsyncGenerator[None, None]:
            yield None

    _pr_settings = FakeSettingsReader({"general.property_refresh_interval_sec": 1})
    _pr_grid = Mock()
    _pr_publisher = AsyncMock()

    mock_property_refresh_svc = Mock()
    mock_property_refresh_svc.refresh_all_properties = AsyncMock(side_effect=RuntimeError("boom"))

    _pr_maintenance = MaintenanceService(settings=FakeSettingsReader({}))
    _pr_crud = DeviceCrudService(settings=_pr_settings, identity=DeviceIdentityConflictService())
    loop = PropertyRefreshLoop(
        services=DeviceServices(
            fleet_capacity=FleetCapacityService(grid=_pr_grid),
            data_cleanup=DataCleanupService(publisher=_pr_publisher, settings=_pr_settings),
            property_refresh=mock_property_refresh_svc,
            groups=DeviceGroupsService(publisher=_pr_publisher, settings=_pr_settings, crud=_pr_crud),
            maintenance=_pr_maintenance,
            bulk=BulkOperationsService(
                publisher=_pr_publisher,
                settings=_pr_settings,
                circuit_breaker=Mock(),
                maintenance=_pr_maintenance,
                crud=_pr_crud,
                operator=OperatorNodeLifecycleService(settings=_pr_settings),
            ),
            presenter=DevicePresenterService(settings=_pr_settings),
            test_data=TestDataService(publisher=_pr_publisher),
            portability_export=PortabilityExportService(),
            inventory_export=InventoryExportService(),
            verification=VerificationService(),
            crud=_pr_crud,
            capability=DeviceCapabilityService(),
            connectivity=ConnectivityService(
                publisher=_pr_publisher,
                settings=_pr_settings,
                circuit_breaker=Mock(),
                lifecycle_policy=AsyncMock(),
                health=AsyncMock(),
            ),
            publisher=_pr_publisher,
            settings=_pr_settings,
            grid=_pr_grid,
            session_factory=AsyncMock(),
            circuit_breaker=Mock(),
            health=AsyncMock(),
            lifecycle_incidents=LifecycleIncidentService(),
        )
    )

    with (
        patch("app.devices.services.property_refresh.observe_background_loop", return_value=_Observation()),
        patch("app.devices.services.property_refresh.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        patch("app.devices.services.property_refresh.logger.exception") as log_exception,
        pytest.raises(asyncio.CancelledError),
    ):
        await loop.run()

    log_exception.assert_called_once_with("Property refresh cycle failed")
