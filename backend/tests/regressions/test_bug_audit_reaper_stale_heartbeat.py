"""Bug 1: Run reaper expires runs without re-checking heartbeat after lock.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-1``.

The reaper snapshots ``last_heartbeat`` outside the row lock at
``service_reaper.py:24-26``, then calls ``expire_run`` which only
guards against terminal state — never re-checks ``last_heartbeat``
under the FOR UPDATE. A concurrent heartbeat refresh between the
snapshot and the lock is silently overridden, killing an active run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.runs import service_reaper as _service_reaper
from app.runs.models import RunState, TestRun
from app.runs.service_reaper import reap_stale_runs

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_reaper_expires_run_after_concurrent_heartbeat_refresh(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    stale_run = TestRun(
        name="reaper-race",
        created_by="qa",
        state=RunState.active,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=300),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
        created_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    db_session.add(stale_run)
    await db_session.commit()
    run_id = stale_run.id

    original_lock = _service_reaper.get_run_for_update  # type: ignore[attr-defined]

    async def _refresh_then_lock(db: object, rid: object) -> TestRun | None:
        # Simulate a concurrent heartbeat that refreshes ``last_heartbeat``
        # between the reaper's unlocked SELECT and its own locked re-fetch.
        # Commit on a side-channel session so the reaper's session
        # (read-committed) sees the fresh value when it acquires the lock.
        async with db_session_maker() as side:
            row = await side.get(TestRun, rid)
            assert row is not None
            row.last_heartbeat = datetime.now(UTC)
            await side.commit()
        return await original_lock(db, rid)  # type: ignore[arg-type]

    with patch.object(_service_reaper, "get_run_for_update", side_effect=_refresh_then_lock):
        await reap_stale_runs(db_session, lifecycle=AsyncMock())

    # Re-read the run on a fresh session so we observe the persisted state,
    # not the in-memory ORM cache.
    async with db_session_maker() as side:
        refreshed = await side.get(TestRun, run_id)
        assert refreshed is not None
        # Fixed behavior: heartbeat was refreshed under lock; run stays active.
        # Current behavior (bug): reaper expires the run anyway.
        assert refreshed.state == RunState.active, (
            f"Reaper expired run despite fresh heartbeat: state={refreshed.state}"
        )
