import asyncio
import contextlib

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceOperationalState
from app.models.host import Host
from app.services import bulk_service, device_locking
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_bulk_start_nodes_uses_per_task_sessions(
    monkeypatch: pytest.MonkeyPatch,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """`bulk_start_nodes` must give each device task its own AsyncSession so
    that one device's intermediate commit does not release `FOR UPDATE` on
    the other devices in the batch."""

    device_a = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-share-a",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    device_b = await create_device(
        db_session,
        host_id=db_host.id,
        name="bulk-share-b",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    device_a_id = device_a.id
    device_b_id = device_b.id

    b_manager_entered = asyncio.Event()
    a_committed = asyncio.Event()
    release_b = asyncio.Event()
    racer_acquired_b = asyncio.Event()

    async def fake_start_node(db: AsyncSession, dev: Device) -> AppiumNode:
        if dev.id == device_b_id:
            # The bulk helper calls the service only after acquiring B's
            # row lock. Holding here makes the lock window observable.
            b_manager_entered.set()
            await release_b.wait()
        elif dev.id == device_a_id:
            await asyncio.wait_for(b_manager_entered.wait(), timeout=3.0)
            # Simulate mark_node_started's intermediate commit. In the
            # buggy shared-session path, this releases A and B's locks.
            await db.commit()
            a_committed.set()

        node = AppiumNode(
            device_id=dev.id,
            port=4720 + (1 if dev.id == device_a_id else 2),
            grid_url="http://grid.example.test:4444",
            state=NodeState.running,
        )
        db.add(node)
        await db.flush()
        return node

    monkeypatch.setattr(bulk_service, "start_node", fake_start_node)

    async def racer() -> None:
        await a_committed.wait()
        async with db_session_maker() as racer_db:
            try:
                # Pre-fix: the shared db committed in FakeNodeManager above
                # released device_b's FOR UPDATE; this lock returns immediately.
                # Post-fix: device_b's per-task session still holds the lock,
                # so this should block until release_b lets B commit.
                locked = await asyncio.wait_for(
                    device_locking.lock_device(racer_db, device_b_id),
                    timeout=0.3,
                )
                racer_acquired_b.set()
                # Touch the row so the lock attempt is observable.
                locked.operational_state = DeviceOperationalState.offline
                await racer_db.commit()
            except TimeoutError:
                # Expected post-fix: per-task session holds B's lock during A's gate.
                with contextlib.suppress(Exception):
                    await racer_db.rollback()

    async def runner() -> dict[str, object]:
        return await bulk_service.bulk_start_nodes(db_session, [device_a_id, device_b_id])

    runner_task = asyncio.create_task(runner())
    racer_task = asyncio.create_task(racer())

    await asyncio.wait_for(a_committed.wait(), timeout=3.0)
    # Give the racer a chance to attempt B's lock under the gate.
    await asyncio.sleep(0.5)
    release_b.set()
    result, _ = await asyncio.wait_for(asyncio.gather(runner_task, racer_task), timeout=10.0)

    assert not racer_acquired_b.is_set(), (
        "racer acquired device_b's FOR UPDATE while bulk_start_nodes was "
        "still in flight - the shared AsyncSession's intermediate commit "
        "released every locked row in the batch. Each per-task call must "
        "use its own AsyncSession so locks stay scoped per device."
    )
    assert result["failed"] == 0, f"bulk_start_nodes reported failures: {result}"

    async with db_session_maker() as verify:
        device_b_row = (await verify.execute(select(Device).where(Device.id == device_b_id))).scalar_one()
    assert device_b_row.operational_state != DeviceOperationalState.offline, (
        "racer's offline write landed on device_b - confirms shared-session lock release"
    )
