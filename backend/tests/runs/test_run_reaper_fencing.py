"""Phase 10 task 6: run-reaper candidate isolation.

Before this change ``reap_stale_runs`` shared one implicit transaction across
every candidate and had no per-candidate exception handling at all: a
``lifecycle.expire_run`` failure (or any exception raised while processing one
run) propagated straight out of ``reap_stale_runs``, aborting the whole
janitor stage and starving every candidate ordered after the failing one.
This module pins the fenced replacement: each candidate gets its own
``db.begin()``, a failure is caught and logged per candidate, and
``expire_run`` always runs with the candidate's transaction already closed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

from app.runs import service_reaper as _service_reaper
from app.runs.models import RunState, TestRun
from app.runs.service_reaper import reap_stale_runs
from tests.concurrency.group_lock_helpers import capture_statements

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _stale_run(run_id: uuid.UUID, name: str, *, now: datetime) -> TestRun:
    return TestRun(
        id=run_id,
        name=name,
        created_by="qa",
        state=RunState.active,
        requirements=[],
        last_heartbeat=now - timedelta(seconds=300),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
        created_at=now - timedelta(seconds=30),
    )


async def test_candidate_failure_does_not_block_later_candidates(db_session: AsyncSession) -> None:
    low, mid, high = sorted(uuid.uuid4() for _ in range(3))
    now = datetime.now(UTC)
    # Seeded in reverse UUID order: the reaper must sort candidates itself, not
    # rely on the discovery scan's incidental row order.
    db_session.add_all(
        [
            _stale_run(high, "reaper-fence-c", now=now),
            _stale_run(mid, "reaper-fence-b", now=now),
            _stale_run(low, "reaper-fence-a", now=now),
        ]
    )
    await db_session.commit()

    calls: list[uuid.UUID] = []

    async def _flaky_expire(run_id: uuid.UUID, reason: str) -> None:
        calls.append(run_id)
        if run_id == mid:
            raise RuntimeError("boom-mid")

    lifecycle = Mock()
    lifecycle.expire_run = AsyncMock(side_effect=_flaky_expire)

    await reap_stale_runs(db_session, lifecycle=lifecycle)

    assert calls == [low, mid, high]


async def test_expire_run_runs_with_no_open_transaction(db_session: AsyncSession) -> None:
    """Forward guard: a naive refactor that forgot to close the candidate's
    ``db.begin()`` before calling ``expire_run`` would fail this even though it
    would not fail against the pre-refactor code (which also released its lock
    via an explicit ``db.commit()`` right before calling ``expire_run``)."""
    stale_run = TestRun(
        name="reaper-fence-notxn",
        created_by="qa",
        state=RunState.active,
        requirements=[],
        last_heartbeat=datetime.now(UTC) - timedelta(seconds=300),
        heartbeat_timeout_sec=60,
        ttl_minutes=60,
    )
    db_session.add(stale_run)
    await db_session.commit()

    observed: list[bool] = []

    async def _spy_expire(run_id: uuid.UUID, reason: str) -> None:
        observed.append(db_session.in_transaction())

    lifecycle = Mock()
    lifecycle.expire_run = AsyncMock(side_effect=_spy_expire)

    await reap_stale_runs(db_session, lifecycle=lifecycle)

    assert observed == [False]


async def test_terminal_race_issues_no_dml_and_never_expires(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    """A run that completes between the unlocked discovery scan and the
    candidate's own lock must be left alone: no expire call, and the locked
    recheck itself performs no DML (nothing to roll back, nothing to commit
    beyond dropping the lock)."""
    stale_run = TestRun(
        name="reaper-fence-terminal-race",
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

    original_lock = _service_reaper.get_run_for_update

    async def _complete_then_lock(db: object, rid: object) -> TestRun | None:
        async with db_session_maker() as side:
            row = await side.get(TestRun, rid)
            assert row is not None
            row.state = RunState.completed
            await side.commit()
        return await original_lock(db, rid)  # type: ignore[arg-type]

    lifecycle = Mock()
    lifecycle.expire_run = AsyncMock()

    # Pinned to db_session's own connection: the concurrent completion above
    # runs on a side-channel session, and a bare engine-level listener would
    # also pick up its UPDATE, which is not the statement this asserts on.
    async with capture_statements(db_session) as statements:
        with patch.object(_service_reaper, "get_run_for_update", side_effect=_complete_then_lock):
            await reap_stale_runs(db_session, lifecycle=lifecycle)

    lifecycle.expire_run.assert_not_awaited()
    dml = [
        statement for statement in statements if statement.strip().upper().startswith(("UPDATE", "DELETE", "INSERT"))
    ]
    assert dml == []

    async with db_session_maker() as verify:
        refreshed = await verify.get(TestRun, run_id)
        assert refreshed is not None
        assert refreshed.state == RunState.completed
