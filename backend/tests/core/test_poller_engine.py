"""The outbox poller's engine: one connection, every statement bounded."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.database import build_poller_engine
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.db


async def test_the_poller_engine_carries_a_command_timeout() -> None:
    """The bound is on the connection, not wrapped around each call.

    A wrapper is a guard someone must remember to apply to every new await on
    the poll path. ``command_timeout`` cannot be forgotten. Introspects the
    live asyncpg connection's own config rather than trusting the constructor
    argument was actually threaded through -- ``connect_args`` is a plain dict
    a future refactor could silently drop a key from.
    """
    command_timeout = 1.5
    engine = build_poller_engine(command_timeout=command_timeout, database_url=TEST_DATABASE_URL)
    try:
        assert engine.dialect.__class__.__name__.startswith("PGDialect")
        assert engine.pool.size() == 1, "the poller needs one connection, not a share of the app pool"
        assert engine.url.render_as_string(hide_password=False) == TEST_DATABASE_URL
        async with engine.connect() as conn:
            raw = await conn.get_raw_connection()
            assert raw.driver_connection._config.command_timeout == command_timeout
    finally:
        await engine.dispose()


async def test_a_slow_statement_is_cancelled_and_the_engine_still_works() -> None:
    """Inject a real slow statement, not a mock.

    A patched method leaves the session clean; a real ``pg_sleep`` exercises the
    actual cancel path, which is the thing that has to leave the poller able to
    make progress on the next iteration.
    """
    engine = build_poller_engine(command_timeout=0.5, database_url=TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            with pytest.raises(Exception) as excinfo:
                await conn.execute(text("SELECT pg_sleep(5)"))
        exc_str = str(excinfo.value).lower()
        exc_type = type(excinfo.value).__name__.lower()
        assert "timeout" in exc_str or "cancel" in exc_str or "timeout" in exc_type or "cancel" in exc_type, (
            f"expected a timeout/cancellation, got {excinfo.value!r}"
        )

        # The property that matters: the next statement works. Whether the
        # connection survived the cancel or was discarded and replaced is the
        # pool's business, but progress afterwards is not optional.
        async with engine.connect() as conn:
            assert (await conn.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await engine.dispose()
