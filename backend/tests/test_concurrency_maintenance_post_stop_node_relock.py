import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import device_locking, maintenance_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_enter_maintenance_relocks_after_stop_node_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-relock",
        availability_status=DeviceAvailabilityStatus.available,
        verified=True,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            pid=12345,
            state=NodeState.running,
        )
    )
    await db_session.commit()
    device_id = device.id

    original_lock_device = device_locking.lock_device
    stop_committed = asyncio.Event()
    relock_seen = asyncio.Event()

    async def observed_lock_device(
        db: AsyncSession,
        target_id: uuid.UUID,
        *,
        load_sessions: bool = False,
    ) -> Device:
        locked = await original_lock_device(db, target_id, load_sessions=load_sessions)
        if target_id == device_id and stop_committed.is_set():
            relock_seen.set()
        return locked

    monkeypatch.setattr(device_locking, "lock_device", observed_lock_device)

    async def stop_node_commits(db: AsyncSession, device: Device) -> AppiumNode:
        assert device.appium_node is not None
        node = device.appium_node
        node.state = NodeState.stopped
        node.pid = None
        device.availability_status = DeviceAvailabilityStatus.offline
        await db.commit()
        stop_committed.set()
        return node

    monkeypatch.setattr(maintenance_service, "stop_node", stop_node_commits)

    async def runner() -> None:
        async with db_session_maker() as session:
            target = await original_lock_device(session, device_id)
            await maintenance_service.enter_maintenance(session, target, drain=False)

    runner_task = asyncio.create_task(runner())
    try:
        await asyncio.wait_for(stop_committed.wait(), timeout=1)
        await asyncio.wait_for(relock_seen.wait(), timeout=1)
    except TimeoutError as exc:
        raise AssertionError("enter_maintenance did not re-lock Device after stop_node committed") from exc
    finally:
        await runner_task

    async with db_session_maker() as verify:
        final_status = (
            await verify.execute(select(Device.availability_status).where(Device.id == device_id))
        ).scalar_one()

    assert final_status == DeviceAvailabilityStatus.maintenance
