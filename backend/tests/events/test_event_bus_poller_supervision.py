"""Track A: the poller is bounded per statement, named, and observable.

The bound is per *statement*, not per iteration. ``_scan_window`` walks the
whole interval above the frontier in one iteration and discards its page cursor
on failure, so an iteration-level timeout shorter than a recovery scan restarts
the identical work forever. See the spec's "Why the statement and not the
iteration".
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, cast
from unittest.mock import patch

import pytest

from app.events.event_bus import EventBus
from tests.helpers import drain_handlers

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

pytestmark = pytest.mark.db


async def test_the_poll_path_uses_the_poller_session_factory(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A configured poller factory is what the poll body opens sessions from.

    Without this the ``command_timeout`` engine added later would be built,
    disposed, and never actually used by a single statement.
    """
    opened = 0

    def counting_factory() -> AsyncSession:
        nonlocal opened
        opened += 1
        return db_session_maker()

    bus = EventBus()
    bus.configure(
        session_factory=db_session_maker,
        engine=cast("AsyncEngine", db_session.bind),
        poller_session_factory=cast("async_sessionmaker[AsyncSession]", counting_factory),
    )

    await bus._dispatch_missed_events()

    assert opened == 1, f"poll body opened {opened} session(s) from the poller factory, expected 1"


async def test_the_poll_path_falls_back_to_the_shared_factory(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A bus configured without a poller factory still polls.

    Every test that builds a bus directly, and any deployment running the
    in-memory fallback, configures only ``session_factory``.
    """
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))

    await bus._dispatch_missed_events()

    assert bus._poll_session_factory is db_session_maker


async def test_the_poller_task_is_named(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """An unnamed task is an unreadable task dump during an incident."""
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    await bus.start()
    try:
        assert bus._poller_task is not None
        assert bus._poller_task.get_name() == "system_event_poller"
    finally:
        await bus.shutdown()


async def test_a_backlog_of_several_pages_drains_in_one_poll(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """One poll drains every page, not one page per poll.

    This is the property a per-iteration timeout would break: ``_scan_window``
    discards its cursor on failure and the frontier is written only after the
    scan returns, so an iteration killed mid-backlog restarts the identical work
    from the same frontier. This test does not reproduce that livelock -- there
    is no per-iteration bound in the tree to reproduce it against. It pins the
    behaviour that makes the livelock possible, so a future change to a
    per-iteration bound has to confront it.

    ``POLL_SCAN_CHUNK_SIZE`` is patched down so the multi-page path runs on
    seven rows instead of a thousand.
    """
    bus = EventBus()
    received: list[object] = []
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    bus.register_handler(received.append)

    row_ids = []
    for index in range(7):
        async with db_session_maker() as db:
            row = bus.queue_for_session(db, "device.updated", {"device_id": f"dev-{index}"})
            assert row is not None
            await db.flush()
            row_ids.append(int(row.id))
            await db.commit()

    bus._last_seen_system_event_id = row_ids[0] - 1

    with patch("app.events.event_bus.POLL_SCAN_CHUNK_SIZE", 3):
        await bus._dispatch_missed_events()

    await drain_handlers(bus)

    assert len(received) == 7, f"expected all 7 rows in one poll, got {len(received)}"
    assert bus._last_seen_system_event_id == row_ids[-1], (
        "the frontier must clear the whole backlog in one poll, not one page"
    )


async def test_the_poll_age_gauge_resets_on_a_successful_poll(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A poller that fails rather than hangs is invisible without this.

    The statement bound stops a wedge; it says nothing about a poll that raises
    every iteration, logs, and delivers nothing.
    """
    from app.core.metrics_recorders import OUTBOX_POLL_AGE_SECONDS
    from app.events.event_bus import refresh_outbox_gauges

    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))

    assert bus._last_successful_poll_at is None
    await bus._dispatch_missed_events()
    assert bus._last_successful_poll_at is not None

    refresh_outbox_gauges(bus)
    assert OUTBOX_POLL_AGE_SECONDS._value.get() < 5.0


async def test_the_gap_gauge_reports_the_pending_set_size(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Both documented steady-state-only bounds become observable, or neither is."""
    from app.core.metrics_recorders import OUTBOX_PENDING_GAPS
    from app.events.event_bus import refresh_outbox_gauges

    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    bus._pending_gaps = {10_001: time.monotonic(), 10_002: time.monotonic()}

    refresh_outbox_gauges(bus)

    assert OUTBOX_PENDING_GAPS._value.get() == 2
