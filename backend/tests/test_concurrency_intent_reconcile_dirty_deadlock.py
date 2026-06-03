"""Regression for N9 (live device-state testing): a lock-order-inversion deadlock
between the inline reconcile path and the background dirty-scan reconciler.

The dirty-scan loop locks the ``Device`` row first (``reconcile_device`` ->
``lock_device``, ``intent_reconciler.py:319``) and then deletes the
``DeviceIntentDirty`` row (``intent_reconciler.py:168``): order Device -> Dirty.

The inline ``*_and_reconcile`` paths used to upsert ``DeviceIntentDirty``
(``mark_dirty``) *before* ``reconcile_device`` locked the ``Device`` row: order
Dirty -> Device. Run concurrently on the same device that AB-BA ordering
deadlocks; Postgres aborts one transaction, which (in the live harness) failed an
operator node restart and dropped the relay from the grid mid-session.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.devices import locking as device_locking
from app.devices.models import DeviceIntentDirty
from app.devices.services.intent import IntentService
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_inline_reconcile_does_not_deadlock_with_dirty_scan_delete(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    await db_session.commit()
    async with db_session_maker() as setup:
        device = await create_device(setup, host_id=db_host.id, name="dirty-deadlock-target")
        # Pre-seed the dirty row so the dirty-scan side has a row to delete and the
        # inline side upserts (locks) the same row.
        await IntentService(setup).mark_dirty(device.id, reason="seed")
        await setup.commit()
        device_id = device.id

    device_locked_by_scan = asyncio.Event()
    scan_may_delete = asyncio.Event()

    async def dirty_scan_side() -> None:
        # Emulates the dirty-scan loop's lock order (intent_reconciler.py:165-168):
        # lock the Device row (reconcile_device does this first), then delete the
        # DeviceIntentDirty row.
        async with db_session_maker() as session, session.begin():
            await device_locking.lock_device(session, device_id)
            device_locked_by_scan.set()
            await scan_may_delete.wait()
            await session.execute(delete(DeviceIntentDirty).where(DeviceIntentDirty.device_id == device_id))

    async def inline_reconcile_side() -> None:
        async with db_session_maker() as session, session.begin():
            await IntentService(session).mark_dirty_and_reconcile(device_id, reason="inline", publisher=event_bus)

    scan = asyncio.create_task(dirty_scan_side())
    await asyncio.wait_for(device_locked_by_scan.wait(), timeout=5.0)

    # Start the inline side while the scan side holds the Device lock. Give it time
    # to reach its second lock acquisition; pre-fix it blocks on the Device row while
    # holding the DeviceIntentDirty row, setting up the cycle once the scan deletes.
    inline = asyncio.create_task(inline_reconcile_side())
    await asyncio.sleep(0.3)
    scan_may_delete.set()

    # Pre-fix: one transaction raises asyncpg DeadlockDetectedError. Post-fix (the
    # inline path locks the Device row first): both transactions complete.
    await asyncio.wait_for(asyncio.gather(scan, inline), timeout=15.0)
