"""A poll that starts while another is mid-scan delivers every row exactly once.

``_dispatch_lock`` wraps the whole of ``_dispatch_and_promote``, including the
forward scan, so a second ``_dispatch_missed_events()`` call does not interleave
with a paused first one: it blocks on ``acquire()`` and its body runs strictly
afterwards, re-scanning the same window. What this test pins is that the re-scan
delivers nothing twice and drops nothing -- the dispatch loop's check-then-add
over ``_dispatched_row_ids`` runs with no ``await`` between the membership test
and the dispatch, and that is what makes the second pass a no-op.

What it deliberately does NOT prove: that the lock is load-bearing. Serialising
is what stops the two bodies from overlapping in the first place, so this test
would still pass with the lock removed. Its companion is
``test_event_bus_gaps.py::test_the_lock_keeps_the_frontier_monotonic``, which
pins what the lock does buy: the unconditional ``_last_seen_system_event_id``
write means a body that scanned a while ago holds a stale, lower frontier, and
with the lock bypassed it drags the frontier backwards after a peer advanced it.

No test can fail for want of the lock on the *stranding* property, and none is
coming. This paragraph used to promise one. It was wrong: it assumed the poll
snapshots the watermark before scanning, and it does not. ``_dispatch_new_rows``,
``_record_new_gaps``, the frontier write and the prune are one ``await``-free
block, so nothing can interrupt a body between recording its gaps and writing its
frontier; and ``_record_new_gaps`` reads the watermark live, so a stale body's
gap interval collapses to empty rather than skipping an id. A regressed frontier
therefore costs a re-scan of rows already in the dedupe map -- work
amplification, not data loss.

The pause is a passthrough wrapper that never names ``_scan_window``'s return
shape, so the rewrite's change to that shape leaves this test untouched.
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
async def test_a_second_poll_behind_a_paused_one_delivers_every_row_exactly_once(
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
        # One yield is all that is needed and all that has any effect: ``second``
        # reaches ``async with self._dispatch_lock`` and suspends there until
        # ``first`` releases it. Scheduling it before that release is the point --
        # its body then runs against state ``first`` has already written.
        await asyncio.sleep(0)
        release_first_scan.set()
        await asyncio.gather(first, second)
    await drain_handlers(bus)

    delivered = sorted(event.data["device_id"] for event in received)
    assert delivered == ["d0", "d1", "d2"], f"stranded or duplicated under overlapping polls: {delivered}"
