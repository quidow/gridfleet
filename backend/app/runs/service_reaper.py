from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_reservation import get_run_for_update

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.runs.service_lifecycle import RunLifecycleService

logger = get_logger(__name__)

NON_TERMINAL_STATES = [s for s in RunState if s not in TERMINAL_STATES]


@dataclasses.dataclass(frozen=True)
class RunReapCandidate:
    run_id: UUID


@dataclasses.dataclass(frozen=True)
class RunExpiry:
    run_id: UUID
    reason: str


async def reap_stale_runs(db: AsyncSession, *, lifecycle: RunLifecycleService) -> None:
    """Expire stale test runs (janitor stage; heartbeat timeout or TTL exceeded)."""
    now = now_utc()

    # Postgres make_interval(years, months, weeks, days, hours, mins, secs).
    heartbeat_deadline_expr = TestRun.last_heartbeat + func.make_interval(
        0, 0, 0, 0, 0, 0, TestRun.heartbeat_timeout_sec
    )
    ttl_deadline_expr = TestRun.created_at + func.make_interval(0, 0, 0, 0, 0, TestRun.ttl_minutes)
    stmt = select(TestRun.id).where(
        TestRun.state.in_(NON_TERMINAL_STATES),
        or_(
            and_(TestRun.last_heartbeat.is_not(None), heartbeat_deadline_expr < now),
            ttl_deadline_expr < now,
        ),
    )
    async with db.begin():
        result = await db.execute(stmt)
        candidates = sorted(
            (RunReapCandidate(run_id) for run_id in result.scalars().all()), key=lambda candidate: candidate.run_id
        )

    for candidate in candidates:
        try:
            async with db.begin():
                expiry = await _decide_expiry(db, candidate.run_id, now=now_utc())
            if expiry is not None:
                await lifecycle.expire_run(expiry.run_id, expiry.reason)
        except Exception:
            logger.exception("run_reap_candidate_failed", run_id=str(candidate.run_id))


async def _decide_expiry(db: AsyncSession, run_id: UUID, *, now: datetime) -> RunExpiry | None:
    # Re-check staleness under the row lock. The discovery SELECT above has no
    # FOR UPDATE, so a concurrent ``heartbeat()`` could refresh
    # ``last_heartbeat`` between that snapshot and the lock taken here.
    # Without this re-check the reaper kills runs that just received a fresh
    # heartbeat. The WARN log is deferred until after the lock confirms the
    # run is still stale, so a near-miss does not produce a misleading
    # "Expiring run …" line for a run we ultimately leave alone. The reason
    # string is also picked from the condition still stale under the lock —
    # picking it from the pre-lock snapshot could mislabel a TTL expiry as a
    # heartbeat timeout (or vice versa) when one predicate flipped between the
    # discovery SELECT and the locked re-fetch.
    locked = await get_run_for_update(db, run_id)
    if locked is None:
        return None
    if locked.state in TERMINAL_STATES:
        return None
    heartbeat_stale = _heartbeat_stale(locked, now)
    ttl_stale = _ttl_stale(locked, now)
    if not (heartbeat_stale or ttl_stale):
        return None

    if heartbeat_stale:
        logger.warning(
            "Expiring run %s (%s): heartbeat timeout (last: %s, timeout: %ds)",
            locked.id,
            locked.name,
            locked.last_heartbeat,
            locked.heartbeat_timeout_sec,
        )
        reason = "Heartbeat timeout"
    else:
        logger.warning(
            "Expiring run %s (%s): TTL exceeded (%d minutes)",
            locked.id,
            locked.name,
            locked.ttl_minutes,
        )
        reason = f"TTL exceeded ({locked.ttl_minutes} minutes)"

    return RunExpiry(run_id=locked.id, reason=reason)


def _heartbeat_stale(run: TestRun, now: datetime) -> bool:
    if run.last_heartbeat is None:
        return False
    return now > run.last_heartbeat + timedelta(seconds=run.heartbeat_timeout_sec)


def _ttl_stale(run: TestRun, now: datetime) -> bool:
    if run.created_at is None:
        return False
    return now > run.created_at + timedelta(minutes=run.ttl_minutes)
