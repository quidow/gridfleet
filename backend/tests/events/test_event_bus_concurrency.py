"""Two overlapping polls must strand nothing and duplicate nothing.

``_dispatch_lock`` guards the read-modify-write across the poller's delivery
state. Nothing in production drives two polls at once today -- one poller task
per process calls ``_dispatch_missed_events`` -- so this test fabricates the
interleaving that a doorbell wake would make reachable.

What actually prevents a duplicate is the dedupe map, read and written with no
``await`` between the check and the dispatch; the lock is what keeps the frontier
and the gap set consistent across the awaits in between. The assertion below is
falsifiable against the dedupe map (see the demonstration step in the plan), not
against the lock alone -- stated here so a later reader does not mistake a
passing test for proof that removing the lock is safe.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

from app.events.event_bus import Event, EventBus
from tests.helpers import drain_handlers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker


@pytest.mark.db
async def test_two_overlapping_polls_dispatch_every_row_exactly_once(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)
    # ``bus.start()`` is deliberately not called: with no listener, the two polls
    # are the only deliverers, so a stranded or duplicated row is unambiguous.

    for index in range(3):
        bus.queue_for_session(db_session, "device.updated", {"device_id": f"d{index}"})
    await db_session.commit()

    first_scan_done = asyncio.Event()
    release_first_scan = asyncio.Event()
    scans = 0
    original_scan = bus._scan_window

    async def paused_scan(db: AsyncSession) -> Any:  # noqa: ANN401
        # Passthrough: never names the scan's return shape, so this survives
        # changes to it.
        nonlocal scans
        scans += 1
        result = await original_scan(db)
        if scans == 1:
            first_scan_done.set()
            await release_first_scan.wait()
        return result

    with patch.object(bus, "_scan_window", new=paused_scan):
        first = asyncio.create_task(bus._dispatch_missed_events())
        await first_scan_done.wait()
        second = asyncio.create_task(bus._dispatch_missed_events())
        # Give the second poll every chance to run ahead of the paused first one.
        for _ in range(10):
            await asyncio.sleep(0)
        release_first_scan.set()
        await asyncio.gather(first, second)
    await drain_handlers(bus)

    delivered = sorted(event.data["device_id"] for event in received)
    assert delivered == ["d0", "d1", "d2"], f"stranded or duplicated under overlapping polls: {delivered}"
