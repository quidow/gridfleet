"""Smoke test: concurrent reconciles of the same device do not deadlock.

Both the inline ``reconcile_now`` path and the background reconciler scan take
a single ``Device`` row lock (``reconcile_device`` -> ``lock_device``) and touch
nothing else that needs its own lock ordering. Running them concurrently on the
same device serializes on that one lock — there is no AB-BA cycle to deadlock
(the old ``DeviceIntentDirty`` second lock, and its deadlock class, are gone).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from app.devices import locking as device_locking
from app.devices.services.intent import IntentService
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


async def test_inline_reconcile_does_not_deadlock_with_scan(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    await db_session.commit()
    async with db_session_maker() as setup:
        device = await create_device(setup, host_id=db_host.id, name="reconcile-race-target")
        await setup.commit()
        device_id = device.id

    device_locked_by_scan = asyncio.Event()
    scan_may_proceed = asyncio.Event()

    async def scan_side() -> None:
        # Emulates the reconciler scan holding the single Device row lock.
        async with db_session_maker() as session, session.begin():
            await device_locking.lock_device(session, device_id)
            device_locked_by_scan.set()
            await scan_may_proceed.wait()

    async def inline_reconcile_side() -> None:
        async with db_session_maker() as session, session.begin():
            await IntentService(session).reconcile_now(device_id, publisher=event_bus)

    scan = asyncio.create_task(scan_side())
    await asyncio.wait_for(device_locked_by_scan.wait(), timeout=5.0)

    # Start the inline side while the scan side holds the Device lock; it blocks on
    # the same single lock rather than forming a cycle.
    inline = asyncio.create_task(inline_reconcile_side())
    await asyncio.sleep(0.3)
    scan_may_proceed.set()

    # Both transactions complete — no DeadlockDetectedError.
    await asyncio.wait_for(asyncio.gather(scan, inline), timeout=15.0)
