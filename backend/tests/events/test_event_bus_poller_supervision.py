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
from sqlalchemy import event as sa_event

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
        # The seed that stops outbox_poll_age_seconds reading a false-healthy
        # 0.0 for a poller that has never actually succeeded.
        assert bus._last_successful_poll_at is not None
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
    # Seed a deliberately stale value first: a gauge that could not tell this
    # apart from a fresh poll would be no better than the old None-fallback,
    # which pinned at 0.0 regardless of whether a poll had ever succeeded.
    bus._last_successful_poll_at = time.monotonic() - 100.0
    refresh_outbox_gauges(bus)
    stale_age = OUTBOX_POLL_AGE_SECONDS._value.get()
    assert stale_age >= 100.0

    await bus._dispatch_missed_events()
    assert bus._last_successful_poll_at is not None

    refresh_outbox_gauges(bus)
    fresh_age = OUTBOX_POLL_AGE_SECONDS._value.get()
    assert fresh_age < 5.0
    assert fresh_age < stale_age


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


async def test_a_poll_issues_only_reads(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A read-only poll is what lets delivery work against a database in recovery.

    The structural guard for this is a token ban, which cannot see a write that
    arrives through a helper. This taps real statements instead, so a future
    write sneaking onto the poll path fails here.
    """
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))

    async with db_session_maker() as db:
        row = bus.queue_for_session(db, "device.updated", {"device_id": "dev-read-only"})
        assert row is not None
        await db.flush()
        await db.commit()

    bus._pending_gaps = {10_001: time.monotonic()}
    statements: list[str] = []

    def tap(_conn: object, _cursor: object, statement: str, *_args: object, **_kwargs: object) -> None:
        statements.append(statement)

    engine = db_session.bind.sync_engine
    sa_event.listen(engine, "before_cursor_execute", tap)
    try:
        await bus._dispatch_missed_events()
    finally:
        sa_event.remove(engine, "before_cursor_execute", tap)

    assert statements, "the tap caught nothing; the poll did not run"
    # A prefix check alone passes ``SELECT ... FOR UPDATE`` / ``FOR SHARE`` --
    # a locking read, which is the shape a hot standby actually rejects, and
    # which has precedent elsewhere in this codebase (``lock_device``,
    # ``get_device_for_update_or_404``). ``WITH``/``MERGE`` are not checked
    # here -- deliberately deferred, with no such precedent on the poll path.
    writing = [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE", "CREATE", "ALTER", "DROP"))
        or "FOR UPDATE" in statement.upper()
        or "FOR SHARE" in statement.upper()
    ]
    assert not writing, f"the poll path issued write statement(s): {writing}"


async def test_a_hung_gap_lookup_is_caught_and_the_scan_still_advances(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The composition Finding 1 lived in: an injected hang, not a mock.

    ``test_a_slow_statement_is_cancelled_and_the_engine_still_works`` proves a
    hung statement recovers, but never enters the poll body.
    ``test_poll_failure_logging_backs_off_during_an_outage`` enters the poll
    loop, but injects a *connect* failure, not a statement timeout. Neither
    exercises what asyncpg actually raises when ``command_timeout`` fires: a
    bare ``TimeoutError``, not a ``SQLAlchemyError`` -- see the comment at
    ``POLL_STATEMENT_TIMEOUT_SEC``. Before that handler also caught
    ``TimeoutError``, this exact scenario propagated the bare ``TimeoutError``
    out of ``_dispatch_and_promote`` uncaught, so the forward scan below it
    never ran and the frontier never moved.

    ``_GAP_LOOKUP_SQL`` is patched to a real ``pg_sleep`` so the timeout fires
    on the actual command_timeout-bound connection, the same as production --
    the forward scan's own statement is never touched by the patch.

    Builds its own scoped schema rather than using ``build_poller_engine``
    (like the other tests in ``tests/core/test_poller_engine.py``), which has
    no way to pin a search path -- same reason
    ``tests/migrations/test_system_events_notify_trigger.py`` does not use the
    ``setup_database``/``db_session_maker`` fixture chain either. This engine
    needs both ``command_timeout`` and a search path together, on one
    connection.
    """
    import logging
    import uuid

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from app.core.database import Base
    from tests.conftest import TEST_DATABASE_URL

    schema_name = f"test_hung_gap_lookup_{uuid.uuid4().hex}"
    engine = create_async_engine(
        TEST_DATABASE_URL,
        pool_size=1,
        max_overflow=0,
        connect_args={"command_timeout": 0.5, "server_settings": {"search_path": schema_name}},
    )
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    bus = EventBus()
    bus.configure(session_factory=maker, poller_session_factory=maker)

    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, checkfirst=True))

        async with maker() as seed_db:
            row = bus.queue_for_session(seed_db, "device.updated", {"device_id": "dev-hung-gap-lookup"})
            assert row is not None
            await seed_db.flush()
            first_id = int(row.id)
            await seed_db.commit()

        bus._pending_gaps = {first_id: time.monotonic()}
        bus._last_seen_system_event_id = first_id - 1

        with (
            caplog.at_level(logging.WARNING),
            patch("app.events.event_bus._GAP_LOOKUP_SQL", text("SELECT pg_sleep(5)")),
        ):
            await bus._dispatch_missed_events()

        assert bus._last_seen_system_event_id == first_id, (
            "the forward scan must still run and advance the frontier despite the timed-out gap lookup"
        )
        assert first_id not in bus._pending_gaps, "the scan re-discovered the row and cleared it as a gap"
        warnings = [record for record in caplog.records if "Could not resolve outbox gaps" in record.getMessage()]
        assert len(warnings) == 1, f"expected one gap-lookup-failed warning, got {len(warnings)}"

        # The next iteration: the hang is gone, and gap resolution runs the real
        # query again on the same bus. Recovery, not just resilience to one bad tick.
        async with maker() as seed_db:
            row = bus.queue_for_session(seed_db, "device.updated", {"device_id": "dev-next-iteration"})
            assert row is not None
            await seed_db.flush()
            second_id = int(row.id)
            await seed_db.commit()

        bus._pending_gaps = {second_id: time.monotonic()}

        await bus._dispatch_missed_events()

        assert bus._last_seen_system_event_id == second_id
        assert second_id not in bus._pending_gaps
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await engine.dispose()
