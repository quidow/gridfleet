import asyncio
from contextlib import suppress
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.host import Host
from app.services import device_locking, device_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_delete_device_locks_row_before_reading_node_state(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """`delete_device` must serialize its node-state read with node starts."""

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="del-toctou",
        availability_status=DeviceAvailabilityStatus.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4724,
        grid_url="http://grid:4444",
        state=NodeState.stopped,
    )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    delete_read_done = asyncio.Event()
    delete_locked = asyncio.Event()
    proceed_delete = asyncio.Event()
    starter_attempting = asyncio.Event()
    starter_committed = asyncio.Event()
    stop_called = asyncio.Event()

    original_get_device = device_service.get_device
    original_lock_device = device_locking.lock_device

    async def gated_get_device(db: AsyncSession, did: object) -> Device | None:
        device_row = await original_get_device(db, did)  # type: ignore[arg-type]
        if did == device_id:
            delete_read_done.set()
            await proceed_delete.wait()
        return device_row

    async def gated_lock_device(db: AsyncSession, did: object, **kwargs: object) -> Device:
        locked = await original_lock_device(db, did, **kwargs)  # type: ignore[arg-type]
        current = asyncio.current_task()
        if did == device_id and current is not None and current.get_name() == "delete-device-task":
            delete_locked.set()
            await proceed_delete.wait()
        return locked

    async def observed_stop_node(self: object, db: AsyncSession, dev: Device) -> AppiumNode:
        stop_called.set()
        assert dev.appium_node is not None
        dev.appium_node.state = NodeState.stopped
        await db.flush()
        return dev.appium_node

    async def deleter() -> bool:
        async with db_session_maker() as db:
            with (
                patch.object(device_service, "get_device", new=gated_get_device),
                patch.object(device_locking, "lock_device", new=gated_lock_device),
                patch(
                    "app.services.node_manager.RemoteNodeManager.stop_node",
                    new=observed_stop_node,
                ),
            ):
                return await device_service.delete_device(db, device_id)

    async def starter() -> str:
        wait_tasks = [
            asyncio.create_task(delete_read_done.wait()),
            asyncio.create_task(delete_locked.wait()),
        ]
        _, pending = await asyncio.wait(wait_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        async with db_session_maker() as db:
            starter_attempting.set()
            try:
                locked_device = await device_locking.lock_device(db, device_id)
            except NoResultFound:
                return "deleted_before_start"
            assert locked_device.appium_node is not None
            locked_device.appium_node.state = NodeState.running
            await db.commit()
            starter_committed.set()
            return "started"

    delete_task = asyncio.create_task(deleter(), name="delete-device-task")
    starter_task = asyncio.create_task(starter(), name="start-node-task")
    await asyncio.wait_for(starter_attempting.wait(), timeout=5.0)
    with suppress(TimeoutError):
        await asyncio.wait_for(starter_committed.wait(), timeout=0.2)
    proceed_delete.set()
    deleted, starter_result = await asyncio.wait_for(
        asyncio.gather(delete_task, starter_task),
        timeout=5.0,
    )

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
        node_row = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one_or_none()

    if starter_result == "started":
        assert stop_called.is_set(), (
            "delete_device allowed a compliant concurrent node-start to mark "
            "the node running, then deleted the device without stopping the "
            "agent-owned node first"
        )
    else:
        assert starter_result == "deleted_before_start"

    if deleted:
        assert device_row is None, "device row survived a successful delete"
        assert node_row is None, "appium_node row stranded after device delete"
    else:
        assert device_row is not None, "delete returned False but device is gone"


async def test_delete_device_rechecks_node_state_after_stop_commit(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A start after stop_node's internal commit must be observed before delete."""

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="del-after-stop-race",
        availability_status=DeviceAvailabilityStatus.available,
    )
    node = AppiumNode(
        device_id=device.id,
        port=4725,
        grid_url="http://grid:4444",
        state=NodeState.running,
    )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    first_stop_committed = asyncio.Event()
    allow_delete_relock = asyncio.Event()
    starter_committed = asyncio.Event()
    stop_calls = 0

    async def observed_stop_node(self: object, db: AsyncSession, dev: Device) -> AppiumNode:
        nonlocal stop_calls

        stop_calls += 1
        assert dev.appium_node is not None
        dev.appium_node.state = NodeState.stopped
        await db.commit()
        if stop_calls == 1:
            first_stop_committed.set()
            await allow_delete_relock.wait()
        return dev.appium_node

    async def deleter() -> bool:
        async with db_session_maker() as db:
            with patch(
                "app.services.node_manager.RemoteNodeManager.stop_node",
                new=observed_stop_node,
            ):
                return await device_service.delete_device(db, device_id)

    async def starter() -> str:
        await first_stop_committed.wait()
        async with db_session_maker() as db:
            try:
                locked_device = await device_locking.lock_device(db, device_id)
            except NoResultFound:
                return "deleted_before_start"
            assert locked_device.appium_node is not None
            locked_device.appium_node.state = NodeState.running
            await db.commit()
            starter_committed.set()
            return "started"

    delete_task = asyncio.create_task(deleter())
    starter_task = asyncio.create_task(starter())
    await asyncio.wait_for(starter_committed.wait(), timeout=5.0)
    allow_delete_relock.set()
    deleted, starter_result = await asyncio.wait_for(
        asyncio.gather(delete_task, starter_task),
        timeout=5.0,
    )

    assert starter_result == "started"
    assert stop_calls >= 2, (
        "delete_device re-locked after stop_node committed but did not re-check "
        "that a concurrent starter had made the node running again"
    )
    assert deleted is True

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
        node_row = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one_or_none()

    assert device_row is None
    assert node_row is None
