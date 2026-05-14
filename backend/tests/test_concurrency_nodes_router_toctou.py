import asyncio
from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import get_db
from app.devices.models import Device, DeviceHold, DeviceOperationalState, DeviceReservation
from app.hosts.models import Host
from app.main import app
from tests.helpers import create_device

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

    app.dependency_overrides[get_db] = override_get_db

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
