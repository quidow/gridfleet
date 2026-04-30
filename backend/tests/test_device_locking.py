import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import DeviceAvailabilityStatus
from app.models.host import Host
from app.services.device_locking import lock_device, lock_devices
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_lock_device_returns_row_with_for_update(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="lock-target")
    await db_session.commit()

    locked = await lock_device(db_session, device.id)

    assert locked.id == device.id
    assert locked.availability_status == DeviceAvailabilityStatus.offline


async def test_lock_devices_orders_by_id_to_avoid_deadlock(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    a = await create_device(db_session, host_id=db_host.id, name="device-a")
    b = await create_device(db_session, host_id=db_host.id, name="device-b")
    await db_session.commit()

    locked = await lock_devices(db_session, [b.id, a.id])

    assert [d.id for d in locked] == sorted([a.id, b.id])


async def test_lock_device_blocks_concurrent_writer(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    # db_host is flushed but not committed by the fixture; commit so
    # the host row is visible to other sessions opened below.
    await db_session.commit()

    async with db_session_maker() as setup:
        device = await create_device(setup, host_id=db_host.id, name="contention-target")
        await setup.commit()
        device_id = device.id

    started = asyncio.Event()
    release = asyncio.Event()
    second_finished = asyncio.Event()
    log: list[str] = []

    async def first_holder() -> None:
        async with db_session_maker() as session, session.begin():
            await lock_device(session, device_id)
            log.append("first-locked")
            started.set()
            await release.wait()
            log.append("first-committed")

    async def second_acquirer() -> None:
        await started.wait()
        async with db_session_maker() as session, session.begin():
            await lock_device(session, device_id)
            log.append("second-locked")
        second_finished.set()

    first = asyncio.create_task(first_holder())
    second = asyncio.create_task(second_acquirer())

    await started.wait()
    await asyncio.sleep(0.2)
    assert log == ["first-locked"], "second acquirer must block while first holds the lock"

    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)
    assert log == ["first-locked", "first-committed", "second-locked"]
