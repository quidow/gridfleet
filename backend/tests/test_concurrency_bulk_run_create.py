# backend/tests/test_concurrency_bulk_run_create.py
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
from tests.helpers import create_device

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
            availability_status=DeviceAvailabilityStatus.available,
            verified=True,
        )
        for i in range(3)
    ]
    await db_session.commit()
    device_ids = [str(d.id) for d in devices]

    bulk_path = "/api/devices/bulk/enter-maintenance"

    async def bulk_maintenance() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.post(
                    bulk_path,
                    json={"device_ids": device_ids, "drain": False},
                )
                return resp.status_code
        finally:
            app.dependency_overrides.pop(get_db, None)

    async def run_create() -> int:
        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with db_session_maker() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
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
        assert device_row.availability_status == DeviceAvailabilityStatus.reserved, (
            f"Device {device_row.id} has active reservation but status is {device_row.availability_status} "
            f"— orphaned reservation. HTTP statuses were {statuses}."
        )
