# backend/tests/test_concurrency_bulk_run_create.py
import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import Mock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db
from app.devices.dependencies import get_device_services
from app.devices.models import Device, DeviceHold, DeviceOperationalState, DeviceReservation
from app.devices.services.bulk import BulkOperationsService
from app.devices.services.data_cleanup import DataCleanupService
from app.devices.services.fleet_capacity import FleetCapacityService
from app.devices.services.groups import DeviceGroupsService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.portability_export import PortabilityExportService
from app.devices.services.presenter import DevicePresenterService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.service import DeviceCrudService
from app.devices.services.state import DeviceStateService
from app.devices.services.test_data import TestDataService
from app.devices.services.verification import VerificationService
from app.devices.services_container import DeviceServices
from app.events.dependencies import get_event_services
from app.events.services_container import EventServices
from app.grid.service import GridService
from app.main import app
from app.runs.dependencies import get_run_services
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_query import RunQueryService
from app.runs.services_container import RunServices
from app.settings.dependencies import get_settings_services
from app.settings.service_config import SettingsConfigService
from app.settings.services_container import SettingsServices
from tests.conftest import settings_service, test_circuit_breaker
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

pytestmark = pytest.mark.asyncio


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_bulk_maintenance_does_not_orphan_run_create_reservations(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: object,
) -> None:
    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,  # type: ignore[union-attr]
            name=f"bulk-{i}",
            operational_state=DeviceOperationalState.available,
            verified=True,
        )
        for i in range(3)
    ]
    await db_session.commit()
    device_ids = [str(d.id) for d in devices]

    bulk_path = "/api/devices/bulk/enter-maintenance"

    def _override_event_services() -> EventServices:
        return EventServices(  # type: ignore[arg-type]
            publisher=event_bus,
            subscriber=event_bus,
            reader=event_bus,
            session_factory=db_session_maker,
            engine=db_session_maker.kw["bind"],
        )

    async def bulk_maintenance() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        def _override_device_services() -> DeviceServices:
            sf = async_sessionmaker(db_session_maker.kw["bind"], class_=AsyncSession, expire_on_commit=False)
            _grid_svc = GridService(settings=settings_service)
            _maintenance_svc = MaintenanceService(publisher=event_bus)
            _crud_svc = DeviceCrudService(settings=settings_service)
            return DeviceServices(
                state=DeviceStateService(publisher=event_bus),
                fleet_capacity=FleetCapacityService(grid=_grid_svc),
                data_cleanup=DataCleanupService(publisher=event_bus, settings=settings_service),
                property_refresh=PropertyRefreshService(discovery=Mock()),
                groups=DeviceGroupsService(publisher=event_bus, settings=settings_service, crud=_crud_svc),
                maintenance=_maintenance_svc,
                bulk=BulkOperationsService(
                    publisher=event_bus,
                    settings=settings_service,
                    circuit_breaker=test_circuit_breaker,
                    maintenance=_maintenance_svc,
                    crud=_crud_svc,
                ),
                presenter=DevicePresenterService(settings=settings_service),
                test_data=TestDataService(publisher=event_bus),
                portability_export=PortabilityExportService(),
                verification=VerificationService(),
                crud=_crud_svc,
                publisher=event_bus,
                settings=settings_service,
                grid=_grid_svc,
                session_factory=sf,
                circuit_breaker=test_circuit_breaker,
            )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_event_services] = _override_event_services
        app.dependency_overrides[get_device_services] = _override_device_services
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    bulk_path,
                    json={"device_ids": device_ids},
                )
                return resp.status_code
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_event_services, None)
            app.dependency_overrides.pop(get_device_services, None)

    async def run_create() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        def override_get_settings_services() -> SettingsServices:
            return SettingsServices(
                service=settings_service,
                config=SettingsConfigService(publisher=event_bus),
                session_factory=db_session_maker,
            )

        def _override_run_services() -> RunServices:
            grid = GridService(settings=settings_service)
            run_release = RunReleaseService(
                publisher=event_bus,
                settings=settings_service,
                grid=grid,
                device_state=DeviceStateService(publisher=event_bus),
            )
            run_lifecycle = RunLifecycleService(
                publisher=event_bus, settings=settings_service, grid=grid, release=run_release
            )
            run_allocator = RunAllocatorService(
                publisher=event_bus,
                settings=settings_service,
                device_state=DeviceStateService(publisher=event_bus),
            )
            run_failure = RunFailureService(
                publisher=event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                maintenance=MaintenanceService(publisher=event_bus),
            )
            run_query = RunQueryService()
            return RunServices(
                allocator=run_allocator,
                lifecycle=run_lifecycle,
                release=run_release,
                failure=run_failure,
                query=run_query,
                settings=settings_service,
                session_factory=db_session_maker,
            )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_settings_services] = override_get_settings_services
        app.dependency_overrides[get_event_services] = _override_event_services
        app.dependency_overrides[get_run_services] = _override_run_services
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    "/api/runs",
                    json={
                        "name": "bulk-race",
                        "requirements": [
                            {"pack_id": devices[0].pack_id, "platform_id": devices[0].platform_id, "count": 2},
                        ],
                    },
                )
                return resp.status_code
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_settings_services, None)
            app.dependency_overrides.pop(get_event_services, None)
            app.dependency_overrides.pop(get_run_services, None)

    statuses = await asyncio.gather(bulk_maintenance(), run_create())

    assert all(s < 500 for s in statuses), f"Server error in concurrent calls: {statuses}"

    async with db_session_maker() as verify:
        reservations = (
            (await verify.execute(select(DeviceReservation).where(DeviceReservation.released_at.is_(None))))
            .scalars()
            .all()
        )
        active_devices = (
            (await verify.execute(select(Device).where(Device.id.in_([d.id for d in devices])))).scalars().all()
        )

    for reservation in reservations:
        device_row = next(d for d in active_devices if d.id == reservation.device_id)
        assert device_row.hold == DeviceHold.reserved, (
            f"Device {device_row.id} has active reservation but status is {device_row.operational_state} "
            f"— orphaned reservation. HTTP statuses were {statuses}."
        )
