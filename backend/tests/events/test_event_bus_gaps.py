"""Gap tracking: the ids a forward scan observed as missing are the stranding candidates.

The poller records them, resolves them by direct lookup on later polls, and
retires them on a time bound derived from Postgres'
``idle_in_transaction_session_timeout``. No transaction id is consulted
anywhere, which is the whole point: two designs that reasoned about transaction
ids shipped and were withdrawn.

This module was originally specified to hold a test showing that two
unserialised ``_dispatch_and_promote`` bodies *strand* a row, with
``_dispatch_lock`` as the thing that prevents it. No such test exists, because
no such strand exists: the hypothesis assumed the poll snapshots the watermark
before its scan, and it does not. ``test_the_lock_keeps_the_frontier_monotonic``
is what replaced it, and its docstring carries the reasoning.
"""

from __future__ import annotations

import asyncio
import logging
import time
from itertools import pairwise
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.exc import SQLAlchemyError

from app.core.metrics_recorders import OUTBOX_GAPS_RETIRED_TOTAL
from app.events.event_bus import Event, EventBus
from tests.helpers import drain_handlers

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db


def _build_bus(db_session: AsyncSession, maker: async_sessionmaker[AsyncSession]) -> tuple[EventBus, list[Event]]:
    """A poller-only bus: ``start()`` is never called, so no listener competes."""
    bus = EventBus()
    bus.configure(session_factory=maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)
    return bus, received


async def _commit_row(bus: EventBus, maker: async_sessionmaker[AsyncSession], device_id: str) -> int:
    """Stage and commit one outbox row in its own transaction; return its id."""
    async with maker() as db:
        row = bus.queue_for_session(db, "device.updated", {"device_id": device_id})
        assert row is not None
        await db.flush()
        row_id = int(row.id)
        await db.commit()
    return row_id


def _scan_that_parks_the_first_caller(
    bus: EventBus, parked: asyncio.Event, release: asyncio.Event
) -> Callable[[AsyncSession], Awaitable[Any]]:
    """Wrap ``_scan_window`` so its first caller stops just after scanning.

    A passthrough: it never names the scan's return shape, so a change to that
    shape leaves every test using it alone. Parking *after* the real scan is what
    leaves the caller holding a frontier that the world can then move past.
    """
    original = bus._scan_window
    calls = 0

    async def scan(db: AsyncSession) -> Any:  # noqa: ANN401
        nonlocal calls
        calls += 1
        mine = calls
        result = await original(db)
        if mine == 1:
            parked.set()
            await release.wait()
        return result

    return scan


def _retirements() -> float:
    return OUTBOX_GAPS_RETIRED_TOTAL._value.get()  # type: ignore[attr-defined]


async def test_pre_xid_row_is_recorded_as_a_gap_and_delivered_after_commit(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A transaction whose first write is the outbox INSERT holds no transaction id.

    ``system_events.id`` comes from the sequence when the INSERT is flushed, but
    PostgreSQL assigns a transaction id only at ``heap_insert``. A transaction
    that wrote nothing before staging its event therefore holds a sequence value
    while being invisible to every xid-based horizon -- the hole no
    ``pg_current_xact_id`` gate could close. Gap tracking closes it without
    consulting any transaction id: the forward scan passes over the id, the poll
    remembers it, and the next poll resolves it by direct lookup.
    """
    bus, received = _build_bus(db_session, db_session_maker)

    async with db_session_maker() as pre_xid, db_session_maker() as visible:
        row = bus.queue_for_session(pre_xid, "device.updated", {"device_id": "pre-xid"})
        assert row is not None
        await pre_xid.flush()  # sequence value taken; nothing committed, no xid required
        pre_xid_id = int(row.id)

        # A higher id must commit, or the frontier never passes pre_xid_id and
        # there is nothing to record.
        bus.queue_for_session(visible, "device.updated", {"device_id": "visible"})
        await visible.commit()

        await bus._dispatch_missed_events()
        await drain_handlers(bus)

        assert [event.data["device_id"] for event in received] == ["visible"]
        assert set(bus._pending_gaps) == {pre_xid_id}
        assert bus._last_seen_system_event_id > pre_xid_id, "the frontier must advance past a recorded gap"

        await pre_xid.commit()
        await bus._dispatch_missed_events()
        await drain_handlers(bus)

    assert sorted(event.data["device_id"] for event in received) == ["pre-xid", "visible"]
    assert bus._pending_gaps == {}


async def test_frontier_advances_every_poll_while_a_transaction_stays_open(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Promotion is unconditional, so a stall costs one dict entry, not a growing window.

    The gate this replaces held the watermark for the whole life of the open
    transaction, and the scan window grew with it. Here the frontier moves on
    every poll and the gap set holds exactly the open id, no matter how many
    polls run.
    """
    bus, received = _build_bus(db_session, db_session_maker)

    async with db_session_maker() as slow:
        row = bus.queue_for_session(slow, "device.updated", {"device_id": "slow"})
        assert row is not None
        await slow.flush()
        slow_id = int(row.id)

        frontiers: list[int] = []
        for index in range(5):
            async with db_session_maker() as fast:
                bus.queue_for_session(fast, "device.updated", {"device_id": f"fast-{index}"})
                await fast.commit()
            await bus._dispatch_missed_events()
            await drain_handlers(bus)
            frontiers.append(bus._last_seen_system_event_id)
            assert set(bus._pending_gaps) == {slow_id}, f"gap set grew on poll {index}: {bus._pending_gaps}"

        assert all(earlier < later for earlier, later in pairwise(frontiers)), (
            f"the frontier did not advance on every poll while a transaction was open: {frontiers}"
        )
        assert [event.data["device_id"] for event in received] == [f"fast-{index}" for index in range(5)]

        await slow.commit()
        await bus._dispatch_missed_events()
        await drain_handlers(bus)

    assert received[-1].data["device_id"] == "slow"
    assert bus._pending_gaps == {}


async def test_aborted_transactions_sequence_value_retires_unresolved(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A rolled-back staging consumes its sequence value permanently.

    Nothing will ever make that id visible, so the gap must retire on the time
    bound rather than be retried forever -- and it must never be dispatched.
    Retirement is what the Postgres idle-transaction timeout makes principled;
    a firing metric means the assumption was wrong.
    """
    bus, received = _build_bus(db_session, db_session_maker)

    async with db_session_maker() as doomed:
        row = bus.queue_for_session(doomed, "device.updated", {"device_id": "doomed"})
        assert row is not None
        await doomed.flush()
        doomed_id = int(row.id)

        async with db_session_maker() as visible:
            bus.queue_for_session(visible, "device.updated", {"device_id": "visible"})
            await visible.commit()

        await bus._dispatch_missed_events()
        await drain_handlers(bus)
        assert set(bus._pending_gaps) == {doomed_id}

        await doomed.rollback()

    before = _retirements()
    with patch("app.events.event_bus.GAP_RETIREMENT_SEC", 0.0), caplog.at_level(logging.WARNING):
        await bus._dispatch_missed_events()
    await drain_handlers(bus)

    assert bus._pending_gaps == {}
    assert [event.data["device_id"] for event in received] == ["visible"]
    assert _retirements() == before + 1
    assert any(str(doomed_id) in record.getMessage() for record in caplog.records)


async def test_gap_resolution_costs_one_statement_regardless_of_gap_count(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """One ``= ANY(:gap_ids)`` lookup, not one query per gap.

    A per-gap loop would make a stall's cost grow with the number of concurrent
    writers, which is exactly what this design promises it does not.
    """
    counts: list[int] = []
    for gap_count in (1, 50):
        bus, _ = _build_bus(db_session, db_session_maker)
        # Ids far above anything this schema will ever hold: they resolve to
        # nothing, which is the expensive shape (no early exit). Stamped now, so
        # none of them is old enough to retire during this poll.
        now = time.monotonic()
        bus._pending_gaps = {10_000 + index: now for index in range(gap_count)}

        statements = 0

        def tap(*_args: object, **_kwargs: object) -> None:
            nonlocal statements
            statements += 1

        engine = db_session.bind.sync_engine
        sa_event.listen(engine, "before_cursor_execute", tap)
        try:
            await bus._dispatch_missed_events()
        finally:
            sa_event.remove(engine, "before_cursor_execute", tap)
        counts.append(statements)

    assert counts[0] == counts[1], f"poll statement count grew with gap-set size: {counts}"


async def test_late_notification_for_a_promoted_row_is_not_redispatched(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Dedupe entries outlive promotion by ``DEDUPE_GRACE_SEC``.

    Dropping them exactly at promotion is what let a late ``NOTIFY`` for an
    already-delivered row re-dispatch it: the listener reloads by id and has no
    frontier of its own to check.
    """
    bus, received = _build_bus(db_session, db_session_maker)

    row = bus.queue_for_session(db_session, "device.updated", {"device_id": "late"})
    assert row is not None
    await db_session.flush()
    row_id = int(row.id)
    await db_session.commit()

    await bus._dispatch_missed_events()
    await drain_handlers(bus)
    assert [event.data["device_id"] for event in received] == ["late"]
    assert bus._last_seen_system_event_id >= row_id, "the frontier did not promote past a dispatched row"

    # The NOTIFY finally lands, after promotion.
    await bus._load_and_dispatch_system_event(row_id)
    await drain_handlers(bus)

    assert [event.data["device_id"] for event in received] == ["late"]


async def test_failed_gap_resolution_keeps_the_gap_set_and_the_frontier(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Gap entries are never dropped on error; the next poll retries the same set."""
    bus, _ = _build_bus(db_session, db_session_maker)
    bus._pending_gaps = {10_001: time.monotonic()}
    frontier_before = bus._last_seen_system_event_id

    with patch.object(bus, "_resolve_pending_gaps", side_effect=SQLAlchemyError("gap lookup failed")):
        await bus._dispatch_missed_events()

    assert set(bus._pending_gaps) == {10_001}
    assert bus._last_seen_system_event_id == frontier_before


async def test_the_lock_keeps_the_frontier_monotonic(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """What ``_dispatch_lock`` actually buys: the frontier only ever moves forward.

    ``_last_seen_system_event_id = frontier`` is an unconditional write, so a body
    that scanned a while ago holds a stale, lower frontier and will write it
    regardless of what happened since. With the lock bypassed a peer can advance
    the frontier inside that window, and the stale body then drags it backwards.
    Through the locked entry point the same interleaving cannot: the second body
    does not scan until the first has finished, so it never holds a stale
    frontier.

    What the lock does **not** buy -- and what the delivery assertions in both
    halves are here to say -- is protection from stranding or from duplication.
    Both hold with the lock bypassed:

    * Stranding is unreachable because ``_dispatch_new_rows``, ``_record_new_gaps``,
      the frontier write and the prune are one ``await``-free block, so nothing can
      interrupt a body between recording its gaps and writing its frontier; and
      because ``_record_new_gaps`` reads ``_last_seen_system_event_id`` live at call
      time rather than from a pre-scan snapshot. A stale body's gap interval
      therefore collapses to empty once a peer has promoted -- it cannot skip an
      id, because the peer that promoted past that id recorded it as a gap in the
      same atomic step.
    * Duplication is prevented by the check-then-record over ``_dispatched_row_ids``,
      which is likewise ``await``-free.

    So a regressed frontier costs a re-scan of a range whose rows are all already
    in the dedupe map: work amplification, not data loss. That is the lock's other
    benefit -- one forward scan and one gap-resolution round trip per tick instead
    of one per concurrent caller.

    Recorded because two hypotheses died here: the test this replaced asserted
    that unserialised polls *strand* a row, which they do not, on the assumption
    that the poll snapshots the watermark before scanning, which it does not.
    """
    bus, received = _build_bus(db_session, db_session_maker)

    first_id = await _commit_row(bus, db_session_maker, "u-first")
    await bus._dispatch_and_promote()
    await drain_handlers(bus)
    assert bus._last_seen_system_event_id == first_id

    parked, release = asyncio.Event(), asyncio.Event()
    frontiers = [bus._last_seen_system_event_id]
    with patch.object(bus, "_scan_window", new=_scan_that_parks_the_first_caller(bus, parked, release)):
        stale = asyncio.create_task(bus._dispatch_and_promote())
        await parked.wait()
        # The parked body has already scanned. It holds frontier == first_id and
        # will write it whenever it resumes, however far the frontier has moved.
        later_ids = [await _commit_row(bus, db_session_maker, f"u-{index}") for index in range(3)]
        await bus._dispatch_and_promote()  # unserialised: takes no lock
        frontiers.append(bus._last_seen_system_event_id)
        release.set()
        await stale
    frontiers.append(bus._last_seen_system_event_id)
    await drain_handlers(bus)

    assert frontiers[1] == later_ids[-1], f"the peer poll did not advance the frontier: {frontiers}"
    assert frontiers[2] < frontiers[1], (
        f"expected the stale unserialised body to drag the frontier backwards, got {frontiers}. "
        "If this passes, the frontier write is no longer unconditional and this test no longer "
        "describes the code -- read _dispatch_and_promote before deleting either."
    )
    assert sorted(event.data["device_id"] for event in received) == ["u-0", "u-1", "u-2", "u-first"], (
        "the regression changed what was delivered"
    )

    # And the cost of the regression is exactly one wasted re-scan: every row in
    # the range is already in the dedupe map, so nothing is delivered twice.
    await bus._dispatch_and_promote()
    await drain_handlers(bus)
    assert sorted(event.data["device_id"] for event in received) == ["u-0", "u-1", "u-2", "u-first"], (
        "the re-scan forced by the regression duplicated a row"
    )
    assert bus._last_seen_system_event_id == later_ids[-1], "the re-scan did not recover the frontier"

    # The identical interleaving through the locked entry point. Seeded from
    # MAX(id) the way ``start()`` seeds a real bus -- left at 0 it would re-deliver
    # the first half's rows, which would say nothing about the lock.
    bus2, received2 = _build_bus(db_session, db_session_maker)
    bus2._last_seen_system_event_id = await bus2._read_latest_row_id()

    guarded_first = await _commit_row(bus2, db_session_maker, "g-first")
    await bus2._dispatch_missed_events()
    await drain_handlers(bus2)
    assert bus2._last_seen_system_event_id == guarded_first

    # Every frontier write, in the order the bodies wrote it -- not three
    # snapshots. The append runs immediately after the body's ``await``-free tail
    # and, under the lock, still inside it, so no write can hide between samples.
    guarded_writes: list[int] = []
    original_promote = bus2._dispatch_and_promote

    async def promote_and_record() -> None:
        await original_promote()
        guarded_writes.append(bus2._last_seen_system_event_id)

    parked2, release2 = asyncio.Event(), asyncio.Event()
    with (
        patch.object(bus2, "_scan_window", new=_scan_that_parks_the_first_caller(bus2, parked2, release2)),
        patch.object(bus2, "_dispatch_and_promote", new=promote_and_record),
    ):
        holder = asyncio.create_task(bus2._dispatch_missed_events())
        await parked2.wait()
        guarded_later = [await _commit_row(bus2, db_session_maker, f"g-{index}") for index in range(3)]
        waiter = asyncio.create_task(bus2._dispatch_missed_events())
        # One yield is all it takes: ``waiter`` reaches ``async with
        # self._dispatch_lock`` and suspends there. It cannot scan -- which is
        # precisely why it cannot end up holding a stale frontier.
        await asyncio.sleep(0)
        assert guarded_writes == [], "a second body promoted while the first held the lock"
        release2.set()
        await asyncio.gather(holder, waiter)
    await drain_handlers(bus2)

    assert guarded_writes == sorted(guarded_writes), (
        f"the locked entry point let the frontier regress: {guarded_first} then {guarded_writes}"
    )
    assert guarded_writes == [guarded_first, guarded_later[-1]], (
        f"expected the parked body to write its own frontier, then the waiter to advance: {guarded_writes}"
    )
    assert bus2._last_seen_system_event_id == guarded_later[-1]
    delivered2 = sorted(event.data["device_id"] for event in received2)
    assert delivered2 == ["g-0", "g-1", "g-2", "g-first"], f"delivery differed under the lock: {delivered2}"


async def test_failed_forward_scan_keeps_dispatched_gap_rows_out_of_a_retry(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A scan failure costs the frontier, not the rows already dispatched this poll."""
    bus, received = _build_bus(db_session, db_session_maker)

    row = bus.queue_for_session(db_session, "device.updated", {"device_id": "resolved-gap"})
    assert row is not None
    await db_session.flush()
    row_id = int(row.id)
    await db_session.commit()
    # Pretend an earlier poll passed over this id while it was uncommitted.
    bus._pending_gaps = {row_id: time.monotonic()}
    frontier_before = bus._last_seen_system_event_id

    with patch.object(bus, "_scan_window", side_effect=SQLAlchemyError("scan failed")), pytest.raises(SQLAlchemyError):
        await bus._dispatch_missed_events()
    await drain_handlers(bus)

    assert [event.data["device_id"] for event in received] == ["resolved-gap"]
    assert bus._last_seen_system_event_id == frontier_before
    assert bus._pending_gaps == {}

    await bus._dispatch_missed_events()
    await drain_handlers(bus)
    assert [event.data["device_id"] for event in received] == ["resolved-gap"], "retry re-dispatched a delivered row"
