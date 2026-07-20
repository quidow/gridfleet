import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db
from app.devices.dependencies import get_device_services
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
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
from app.events.dependencies import get_event_services
from app.events.services_container import EventServices
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService
from app.main import app
from app.runs.dependencies import get_run_services
from app.runs.service_allocator import RunAllocatorService
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_failures import RunFailureService
from app.runs.service_lifecycle_release import RunReleaseService
from app.runs.service_query import RunQueryService
from app.runs.service_reservation import RunReservationService
from app.runs.services_container import RunServices
from app.settings.dependencies import get_settings_services
from app.settings.service_config import SettingsConfigService
from app.settings.services_container import SettingsServices
from tests.conftest import settings_service, test_circuit_breaker
from tests.fakes import build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_run_create_and_maintenance_cannot_overlap(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="contended",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    def _override_event_services() -> EventServices:
        return EventServices(  # type: ignore[arg-type]
            publisher=event_bus,
            subscriber=event_bus,
            reader=event_bus,
        )

    async def maintenance_request() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        def _override_device_services() -> DeviceServices:
            sf = async_sessionmaker(db_session_maker.kw["bind"], class_=AsyncSession, expire_on_commit=False)
            _maintenance_svc = MaintenanceService(
                review=build_review_service(), settings=settings_service, publisher=event_bus
            )
            _crud_svc = DeviceCrudService(identity=DeviceIdentityConflictService(), publisher=event_bus)
            return DeviceServices(
                fleet_capacity=FleetCapacityService(),
                data_cleanup=DataCleanupService(publisher=event_bus, settings=settings_service),
                property_refresh=PropertyRefreshService(discovery=Mock()),
                groups=DeviceGroupsService(
                    publisher=event_bus,
                    crud=_crud_svc,
                ),
                maintenance=_maintenance_svc,
                bulk=BulkOperationsService(
                    publisher=event_bus,
                    settings=settings_service,
                    circuit_breaker=test_circuit_breaker,
                    maintenance=_maintenance_svc,
                    crud=_crud_svc,
                    operator=OperatorNodeLifecycleService(
                        review=build_review_service(), settings=settings_service, publisher=event_bus
                    ),
                ),
                presenter=DevicePresenterService(),
                test_data=TestDataService(publisher=event_bus),
                crud=_crud_svc,
                capability=DeviceCapabilityService(),
                connectivity=ConnectivityService(
                    publisher=event_bus,
                    settings=settings_service,
                    circuit_breaker=test_circuit_breaker,
                    lifecycle_policy=AsyncMock(),
                    health=AsyncMock(),
                ),
                publisher=event_bus,
                settings=settings_service,
                session_factory=sf,
                circuit_breaker=test_circuit_breaker,
                health=AsyncMock(),
            )

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_event_services] = _override_event_services
        app.dependency_overrides[get_device_services] = _override_device_services
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    f"/api/devices/{device_id}/maintenance",
                    json={},
                )
                return resp.status_code
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_event_services, None)
            app.dependency_overrides.pop(get_device_services, None)

    async def run_create_request() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        def override_get_settings_services() -> SettingsServices:
            return SettingsServices(
                service=settings_service,
                config=SettingsConfigService(publisher=event_bus),
            )

        def _override_run_services() -> RunServices:
            run_release = RunReleaseService(
                publisher=event_bus,
                settings=settings_service,
                deferred_stop=AsyncMock(),
            )
            run_lifecycle = RunLifecycleService(publisher=event_bus, settings=settings_service, release=run_release)
            run_allocator = RunAllocatorService(
                publisher=event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
            )
            run_failure = RunFailureService(
                publisher=event_bus,
                settings=settings_service,
                circuit_breaker=test_circuit_breaker,
                maintenance=MaintenanceService(
                    review=build_review_service(), settings=settings_service, publisher=event_bus
                ),
                lifecycle_actions=AsyncMock(),
                reservation=RunReservationService(review=build_review_service()),
                incidents=LifecycleIncidentService(),
            )
            run_query = RunQueryService()
            return RunServices(
                allocator=run_allocator,
                lifecycle=run_lifecycle,
                release=run_release,
                failure=run_failure,
                reservation=RunReservationService(review=build_review_service()),
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
                        "name": "race-run",
                        "requirements": [
                            {"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1},
                        ],
                    },
                )
                return resp.status_code
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_settings_services, None)
            app.dependency_overrides.pop(get_event_services, None)
            app.dependency_overrides.pop(get_run_services, None)

    statuses = await asyncio.gather(maintenance_request(), run_create_request())
    assert all(s < 500 for s in statuses), f"Server error in concurrent calls: {statuses}"

    async with db_session_maker() as verify:
        reservation = (
            await verify.execute(
                select(DeviceReservation).where(
                    DeviceReservation.device_id == device_id,
                    DeviceReservation.released_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    if reservation is not None:
        assert device_row.operational_state_last_emitted != DeviceOperationalState.maintenance, (
            f"Reservation exists but device row is in maintenance — the maintenance path stomped a "
            f"reservation. HTTP statuses were {statuses}."
        )
    elif any(s in (200, 201) for s in statuses):
        # hold is now derived by the reconciler (Task 7+8); check maintenance_reason signal
        from app.devices.services.lifecycle_policy_state import state as ps

        assert ps(device_row).get("maintenance_reason") is not None, (
            f"No reservation but maintenance_reason not set; "
            f"expected maintenance signal because at least one request succeeded. statuses={statuses}"
        )
