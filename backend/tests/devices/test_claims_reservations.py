from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import Device
from app.devices.services.claims import active_reservation_exists, device_is_reserved
from tests.helpers import create_device, create_reservation  # create_reservation added in step 0 if missing

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


async def test_device_is_reserved_false_without_row(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="res-none")
    await db_session.commit()
    assert await device_is_reserved(db_session, device.id) is False


async def test_device_is_reserved_true_with_active_row(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="res-active")
    await create_reservation(db_session, device_id=device.id)  # released_at IS NULL
    await db_session.commit()
    assert await device_is_reserved(db_session, device.id) is True


async def test_active_reservation_exists_clause_filters(db_session: AsyncSession, db_host: Host) -> None:
    reserved = await create_device(db_session, host_id=db_host.id, name="res-yes")
    free = await create_device(db_session, host_id=db_host.id, name="res-no")
    await create_reservation(db_session, device_id=reserved.id)
    await db_session.commit()
    rows = (await db_session.execute(select(Device.id).where(~active_reservation_exists()))).scalars().all()
    assert free.id in rows
    assert reserved.id not in rows
