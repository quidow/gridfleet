from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from app.devices.locking import DEVICE_LOCK_LEDGER_KEY, lock_device_handle, lock_devices
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.db


async def test_lock_handle_stamps_the_ledger(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="ledger-a")
    await db_session.commit()

    await lock_device_handle(db_session, device.id)
    sync = db_session.sync_session
    transaction, ids = sync.info[DEVICE_LOCK_LEDGER_KEY]
    assert transaction is sync.get_transaction()
    assert device.id in ids


async def test_ledger_resets_when_the_transaction_changes(db_session: AsyncSession, db_host: Host) -> None:
    first = await create_device(db_session, host_id=db_host.id, name="ledger-b")
    second = await create_device(db_session, host_id=db_host.id, name="ledger-c")
    await db_session.commit()

    await lock_device_handle(db_session, first.id)
    await db_session.commit()  # ends the stamping transaction

    await db_session.execute(text("SELECT 1"))  # opens a fresh transaction
    await lock_devices(db_session, [second.id])
    _, ids = db_session.sync_session.info[DEVICE_LOCK_LEDGER_KEY]
    assert second.id in ids
    assert first.id not in ids, "a stale entry outlived its transaction"
