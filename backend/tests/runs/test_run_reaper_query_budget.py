"""Phase 10 task 6: run-reaper commit and query budget.

Before this change the reaper shared one transaction across every candidate:
the discovery read had no transaction boundary of its own, so it piggybacked
on whatever transaction the first candidate's locked recheck opened, and every
candidate (terminal no-op, no-longer-stale no-op, or a real expiry) closed
that shared or its own transaction with exactly one ``db.commit()``. Total
commits across *n* stale runs was therefore *n*, not *n + 1*. After isolating
the discovery read behind its own ``db.begin()``, it is always ``n + 1``: one
for the discovery read, one per candidate.

Statement-wise, the discovery read is always exactly one ``SELECT test_runs``
and each candidate's locked recheck (``get_run_for_update``) is exactly one
more against that table (the ``selectinload`` follow-up lands on
``device_reservations``/``devices``, never ``test_runs``) -- both true before
and after this change, so that count alone would not distinguish old from
new; it is pinned here as a forward regression guard, not RED evidence. The
commit-count assertion below is the one that actually fails against the
pre-refactor code.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import delete, event

from app.runs.models import RunState, TestRun
from app.runs.service_reaper import reap_stale_runs
from tests.bench_instrumentation import CommitTap, QueryTap

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

FLEET_SIZES = (1, 10, 50)


async def _seed_stale_runs(db_session: AsyncSession, n: int, generation: int) -> None:
    now = datetime.now(UTC)
    db_session.add_all(
        [
            TestRun(
                name=f"reaper-budget-{generation}-{i}",
                created_by="qa",
                state=RunState.active,
                requirements=[],
                last_heartbeat=now - timedelta(seconds=300),
                heartbeat_timeout_sec=60,
                ttl_minutes=60,
                created_at=now - timedelta(seconds=30),
            )
            for i in range(n)
        ]
    )
    await db_session.commit()


async def test_reaper_commit_and_query_budget_scales_with_candidates(db_session: AsyncSession) -> None:
    assert db_session.bind is not None
    engine = db_session.bind.sync_engine

    commits: dict[int, int] = {}
    test_run_reads: dict[int, int] = {}
    for generation, n in enumerate(FLEET_SIZES):
        # The mocked lifecycle never actually expires a run, so a prior
        # generation's candidates would otherwise still be non-terminal and
        # stale, and get rediscovered (and re-counted) on every later
        # generation's scan.
        await db_session.execute(delete(TestRun))
        await db_session.commit()
        await _seed_stale_runs(db_session, n, generation)
        lifecycle = AsyncMock()
        tap = QueryTap()
        commit_tap = CommitTap()
        # Engine-scoped on purpose: counts engine-level commits via CommitTap,
        # which the session-pinned helper cannot see. The listeners are
        # attached only around the measured call, so no seeding or teardown
        # traffic is counted. See tests/concurrency/group_lock_helpers.
        # capture_statements for the pinned form the session-scoped budget
        # tests use.
        event.listen(engine, "before_cursor_execute", tap)
        event.listen(engine, "commit", commit_tap)
        try:
            await reap_stale_runs(db_session, lifecycle=lifecycle)
        finally:
            event.remove(engine, "before_cursor_execute", tap)
            event.remove(engine, "commit", commit_tap)

        assert lifecycle.expire_run.await_count == n
        commits[n] = commit_tap.count
        test_run_reads[n] = tap.counter["SELECT test_runs"]

    assert commits == {n: n + 1 for n in FLEET_SIZES}, (
        f"expected one commit for the discovery read plus one per candidate, got {commits}"
    )
    assert test_run_reads == {n: n + 1 for n in FLEET_SIZES}, (
        f"expected one discovery SELECT plus one locked recheck per candidate, got {test_run_reads}"
    )


def test_run_reaper_owns_no_direct_commit_or_rollback() -> None:
    source = (Path(__file__).resolve().parents[2] / "app" / "runs" / "service_reaper.py").read_text(encoding="utf-8")
    assert ".commit()" not in source, "service_reaper.py must not own a commit"
    assert ".rollback()" not in source, "service_reaper.py must not own a rollback"
