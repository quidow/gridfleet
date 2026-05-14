import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.devices import locking as device_locking
from app.hosts.models import Host
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_lock_appium_node_for_device_returns_none_when_missing(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="no-node", verified=True)
    await db_session.commit()

    locked = await appium_node_locking.lock_appium_node_for_device(db_session, device.id)
    assert locked is None


async def test_lock_appium_node_for_device_returns_locked_row(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="with-node", verified=True)
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            grid_url="http://hub:4444",
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
            pid=0,
            active_connection_target="",
        )
    )
    await db_session.commit()

    locked = await appium_node_locking.lock_appium_node_for_device(db_session, device.id)
    assert locked is not None
    assert locked.device_id == device.id


async def test_lock_appium_node_for_device_blocks_concurrent_writer(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Verifies the helper actually emits SELECT … FOR UPDATE on the node row."""
    device = await create_device(db_session, host_id=db_host.id, name="lock-block", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        pid=0,
        active_connection_target="",
    )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    holder_done = asyncio.Event()
    stomper_started = asyncio.Event()

    async def holder() -> None:
        async with db_session_maker() as session:
            await device_locking.lock_device(session, device_id)
            locked_node = await appium_node_locking.lock_appium_node_for_device(session, device_id)
            assert locked_node is not None
            stomper_started.set()
            await asyncio.sleep(0.2)
            locked_node.pid = 222
            await session.commit()
            holder_done.set()

    async def stomper() -> None:
        await stomper_started.wait()
        async with db_session_maker() as session:
            stmt = select(AppiumNode).where(AppiumNode.device_id == device_id)
            stomper_node = (await session.execute(stmt)).scalar_one()
            stomper_node.pid = 333
            await session.commit()

    await asyncio.gather(holder(), stomper())

    async with db_session_maker() as verify:
        verify_node = (await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))).scalar_one()

    assert holder_done.is_set()
    assert verify_node.pid == 333
