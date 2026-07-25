"""Track A: the poller is bounded per statement, named, and observable.

The bound is per *statement*, not per iteration. ``_scan_window`` walks the
whole interval above the frontier in one iteration and discards its page cursor
on failure, so an iteration-level timeout shorter than a recovery scan restarts
the identical work forever. See the spec's "Why the statement and not the
iteration".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from app.events.event_bus import EventBus

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
