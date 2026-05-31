import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.dependencies import get_appium_node_services
from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
from app.appium_nodes.services_container import AppiumNodeServices
from app.core.database import get_db
from app.devices.models import Device, DeviceHold, DeviceOperationalState, DeviceReservation
from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.maintenance import MaintenanceService
from app.devices.services.operator_node_lifecycle import OperatorNodeLifecycleService
from app.devices.services.state import DeviceStateService
from app.events.dependencies import get_event_services
from app.events.services_container import EventServices
from app.grid.service import GridService
from app.hosts.models import Host
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
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


@pytest.mark.usefixtures("seeded_driver_packs")
async def test_start_node_locks_device_before_reservation_check(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """`POST /node/start` must serialize the reservation check with node start."""

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="nodes-toctou",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with db_session_maker() as session:
            yield session

    def override_get_settings_services() -> SettingsServices:
        return SettingsServices(
            service=settings_service,
            config=SettingsConfigService(publisher=event_bus),
            session_factory=db_session_maker,
        )

    def _override_event_services() -> EventServices:
        return EventServices(  # type: ignore[arg-type]
            publisher=event_bus,
            subscriber=event_bus,
            reader=event_bus,
            session_factory=db_session_maker,
            engine=db_session_maker.kw["bind"],
        )

    def _override_run_services() -> RunServices:
        grid = GridService(settings=settings_service)
        run_release = RunReleaseService(
            publisher=event_bus,
            settings=settings_service,
            grid=grid,
            device_state=DeviceStateService(publisher=event_bus),
            deferred_stop=AsyncMock(),
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
            lifecycle_actions=AsyncMock(),
            reservation=RunReservationService(),
            health=AsyncMock(),
        )
        run_query = RunQueryService(capability=DeviceCapabilityService())
        return RunServices(
            allocator=run_allocator,
            lifecycle=run_lifecycle,
            release=run_release,
            failure=run_failure,
            reservation=RunReservationService(),
            query=run_query,
            settings=settings_service,
            session_factory=db_session_maker,
        )

    def _override_appium_node_services() -> AppiumNodeServices:
        return AppiumNodeServices(
            reconciler=AsyncMock(),
            reconciler_agent=ReconcilerAgentService(
                settings=settings_service,
                operator=OperatorNodeLifecycleService(settings=settings_service),
            ),
            node_health=AsyncMock(),
            heartbeat=AsyncMock(),
            settings=settings_service,
            session_factory=db_session_maker,
        )

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_settings_services] = override_get_settings_services
    app.dependency_overrides[get_event_services] = _override_event_services
    app.dependency_overrides[get_run_services] = _override_run_services
    app.dependency_overrides[get_appium_node_services] = _override_appium_node_services

    inside_start = asyncio.Event()
    proceed_start = asyncio.Event()

    original_assert_not_reserved = None

    async def gated_assert_not_reserved(device: Device, db: AsyncSession) -> None:
        inside_start.set()
        await proceed_start.wait()

    try:
        import app.appium_nodes.routers.nodes as nodes_module

        original_assert_not_reserved = nodes_module._assert_device_not_reserved
        nodes_module._assert_device_not_reserved = gated_assert_not_reserved
        with patch.object(nodes_module, "_assert_device_not_reserved", new=gated_assert_not_reserved):

            async def caller_start() -> int:
                async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                    resp = await client.post(f"/api/devices/{device_id}/node/start")
                    return resp.status_code

            async def caller_run_create() -> int:
                await inside_start.wait()
                try:
                    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                        resp = await client.post(
                            "/api/runs",
                            json={
                                "name": "toctou-race",
                                "requirements": [
                                    {
                                        "pack_id": device.pack_id,
                                        "platform_id": device.platform_id,
                                        "count": 1,
                                    },
                                ],
                            },
                        )
                        return resp.status_code
                finally:
                    proceed_start.set()

            start_status, run_status = await asyncio.wait_for(
                asyncio.gather(caller_start(), caller_run_create()),
                timeout=10.0,
            )
    finally:
        if original_assert_not_reserved is not None:
            import app.appium_nodes.routers.nodes as nodes_module

            nodes_module._assert_device_not_reserved = original_assert_not_reserved
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_settings_services, None)
        app.dependency_overrides.pop(get_event_services, None)
        app.dependency_overrides.pop(get_run_services, None)
        app.dependency_overrides.pop(get_appium_node_services, None)

    async with db_session_maker() as verify:
        reservations = (
            (
                await verify.execute(
                    select(DeviceReservation).where(
                        DeviceReservation.device_id == device_id,
                        DeviceReservation.released_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    if reservations:
        assert start_status >= 400, (
            "create_run reserved the device while start_node also succeeded - "
            "split brain. start_node must lock the Device row around the "
            "reservation check, not just before mark_node_started."
        )
    else:
        assert start_status == 200, f"start_node failed unexpectedly: {start_status}"
        assert run_status >= 400, (
            "start_node succeeded but create_run also reserved the device - "
            "the reservation check is racing the node-start window."
        )

    assert device_row.operational_state in {
        DeviceOperationalState.available,
        DeviceHold.reserved,
    }, f"unexpected final status {device_row.operational_state}"
