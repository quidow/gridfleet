import asyncio
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import get_db
from app.main import app
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.device_reservation import DeviceReservation
from app.models.host import Host
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_run_create_and_maintenance_cannot_overlap(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    seeded_driver_packs: None,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="contended",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    await db_session.commit()
    device_id = device.id

    async def maintenance_request() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    f"/api/devices/{device_id}/maintenance",
                    json={"drain": False},
                )
                return resp.status_code
        finally:
            app.dependency_overrides.pop(get_db, None)

    async def run_create_request() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
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

    statuses = await asyncio.gather(maintenance_request(), run_create_request())

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
        assert device_row.availability_status == DeviceAvailabilityStatus.reserved, (
            f"Reservation exists but device row is {device_row.availability_status} — orphaned reservation. "
            f"HTTP statuses were {statuses}."
        )
    else:
        assert device_row.availability_status in {
            DeviceAvailabilityStatus.maintenance,
            DeviceAvailabilityStatus.available,
        }, f"No reservation but device row is {device_row.availability_status}. statuses={statuses}"
