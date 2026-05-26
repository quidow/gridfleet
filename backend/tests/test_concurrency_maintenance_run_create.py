import asyncio
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db
from app.devices.models import Device, DeviceHold, DeviceOperationalState, DeviceReservation
from app.events.dependencies import get_event_services
from app.events.services_container import EventServices
from app.hosts.models import Host
from app.main import app
from app.settings import settings_service
from app.settings.dependencies import get_settings_services
from app.settings.services_container import SettingsServices
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

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
            session_factory=db_session_maker,
            engine=db_session_maker.kw["bind"],
        )

    async def maintenance_request() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_event_services] = _override_event_services
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

    async def run_create_request() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        def override_get_settings_services() -> SettingsServices:
            return SettingsServices(service=settings_service, session_factory=db_session_maker)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_settings_services] = override_get_settings_services
        app.dependency_overrides[get_event_services] = _override_event_services
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
        assert device_row.hold == DeviceHold.reserved, (
            f"Reservation exists but device row is {device_row.operational_state} — orphaned reservation. "
            f"HTTP statuses were {statuses}."
        )
    else:
        if any(s in (200, 201) for s in statuses):
            assert device_row.hold == DeviceHold.maintenance, (
                f"No reservation but device row is {device_row.operational_state}; "
                f"expected maintenance because at least one request succeeded. statuses={statuses}"
            )
