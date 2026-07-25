"""Gap tracking: the ids a forward scan observed as missing are the stranding candidates.

The poller records them, resolves them by direct lookup on later polls, and
retires them on a time bound derived from Postgres'
``idle_in_transaction_session_timeout``. No transaction id is consulted
anywhere, which is the whole point: two designs that reasoned about transaction
ids shipped and were withdrawn.

OPEN QUESTION, awaiting a ruling. This module was also meant to hold
``test_unserialised_polls_strand_a_row_that_the_lock_prevents`` -- the test
``tests/events/test_event_bus_concurrency.py`` promises "lands with that
rewrite", showing that two unserialised ``_dispatch_and_promote`` bodies strand a
row and that ``_dispatch_lock`` is what prevents it. It is absent because its
premise is false against this implementation: three separate interleavings
(pause after the scan, pause between keyset pages, pause inside gap resolution)
all deliver every committed row with the lock bypassed. The reason is structural
-- ``_dispatch_new_rows``, ``_record_new_gaps``, the frontier write and the prune
are one synchronous block, so they are atomic on the event loop, and
``_record_new_gaps`` covers exactly ``(frontier_it_reads, frontier]``. A body can
only "skip" a gap because another body's atomic block already recorded it. The
lock still buys frontier monotonicity and one scan's worth of database work per
tick, but on this evidence it is defence in depth, not the thing that makes gap
tracking correct. Decide whether to keep the lock and say so, or to write a test
of what it does guarantee, before adding a test that asserts a strand.
"""

from __future__ import annotations

import logging
import time
from itertools import pairwise
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy.exc import SQLAlchemyError

from app.core.metrics_recorders import OUTBOX_GAPS_RETIRED_TOTAL
from app.events.event_bus import Event, EventBus
from tests.helpers import drain_handlers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db


def _build_bus(db_session: AsyncSession, maker: async_sessionmaker[AsyncSession]) -> tuple[EventBus, list[Event]]:
    """A poller-only bus: ``start()`` is never called, so no listener competes."""
    bus = EventBus()
    bus.configure(session_factory=maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)
    return bus, received


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
