import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services import state_write_guard
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.intent import IntentService
from app.devices.services.service import DeviceCrudService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


def _crud() -> DeviceCrudService:
    return DeviceCrudService(
        settings=FakeSettingsReader(), identity=DeviceIdentityConflictService(), publisher=event_bus
    )


async def test_delete_device_does_not_wait_for_running_node_to_stop(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """`delete_device` must not block on an observed-running node.

    The node's observed-running flag (`pid` + `active_connection_target`) only
    clears after the agent stops Appium and an observation loop writes the fact
    back — work that never happens inside the request. Delete must remove the
    rows immediately; the leftover agent process is reaped asynchronously by the
    `appium_reconciler` `no_db_row` orphan sweep.

    The `register_intents_and_reconcile` no-op stands in for "the async stop has
    not converged yet": if delete tried to drive the stop and wait for the flag
    to flip, it would spin forever and hit the timeout.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="del-running",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4724,
            desired_state=AppiumDesiredState.running,
            desired_port=4724,
            pid=12345,
            active_connection_target="host:5555",
        )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id
    assert node.observed_running is True

    with patch.object(IntentService, "register_intents_and_reconcile", new=AsyncMock()):
        async with db_session_maker() as db:
            deleted = await asyncio.wait_for(_crud().delete_device(db, device_id), timeout=5.0)
    assert deleted is True

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
        node_row = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one_or_none()
    assert device_row is None, "device row survived delete"
    assert node_row is None, "appium_node row stranded after device delete (cascade missing)"


async def test_delete_device_concurrent_with_node_start_is_consistent(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Delete and a concurrent node-start must serialize on the device row lock
    without deadlock, leaving a consistent end state: a successful delete removes
    both the device and (via cascade) its node row, whichever committed first."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="del-concurrent-start",
        operational_state=DeviceOperationalState.available,
    )
    with state_write_guard.bypass():
        node = AppiumNode(
            device_id=device.id,
            port=4726,
            desired_state=AppiumDesiredState.stopped,
            desired_port=None,
            pid=None,
            active_connection_target=None,
        )
    db_session.add(node)
    await db_session.commit()
    device_id = device.id

    async def deleter() -> bool:
        async with db_session_maker() as db:
            return await _crud().delete_device(db, device_id)

    async def starter() -> str:
        async with db_session_maker() as db:
            try:
                locked = await device_locking.lock_device(db, device_id)
            except NoResultFound:
                return "deleted_before_start"
            assert locked.appium_node is not None
            with state_write_guard.bypass():
                locked.appium_node.pid = 999
            with state_write_guard.bypass():
                locked.appium_node.active_connection_target = "host:5555"
            await db.commit()
            return "started"

    deleted, starter_result = await asyncio.wait_for(
        asyncio.gather(deleter(), starter()),
        timeout=5.0,
    )

    assert starter_result in {"started", "deleted_before_start"}

    async with db_session_maker() as verify:
        device_row = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
        node_row = (
            await verify.execute(select(AppiumNode).where(AppiumNode.device_id == device_id))
        ).scalar_one_or_none()

    if deleted:
        assert device_row is None, "device row survived a successful delete"
        assert node_row is None, "appium_node row stranded after device delete"
    else:
        assert device_row is not None, "delete returned False but device is gone"
