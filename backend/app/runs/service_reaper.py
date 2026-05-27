from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import and_, func, or_, select

from app.core.database import async_session
from app.core.leader.advisory import LeadershipLost, assert_current_leader
from app.core.observability import get_logger, observe_background_loop
from app.runs import service as run_service
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_reservation import get_run_for_update

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)
LOOP_NAME = "run_reaper"

NON_TERMINAL_STATES = [s for s in RunState if s not in TERMINAL_STATES]


def _heartbeat_stale(run: TestRun, now: datetime) -> bool:
    if run.last_heartbeat is None:
        return False
    return now > run.last_heartbeat + timedelta(seconds=run.heartbeat_timeout_sec)


def _ttl_stale(run: TestRun, now: datetime) -> bool:
    if run.created_at is None:
        return False
    return now > run.created_at + timedelta(minutes=run.ttl_minutes)


async def _reap_stale_runs(db: AsyncSession, *, publisher: EventPublisher, settings: SettingsReader) -> None:
    now = datetime.now(UTC)

    # Postgres make_interval(years, months, weeks, days, hours, mins, secs).
    heartbeat_deadline_expr = TestRun.last_heartbeat + func.make_interval(
        0, 0, 0, 0, 0, 0, TestRun.heartbeat_timeout_sec
    )
    ttl_deadline_expr = TestRun.created_at + func.make_interval(0, 0, 0, 0, 0, TestRun.ttl_minutes)
    stmt = select(TestRun).where(
        TestRun.state.in_(NON_TERMINAL_STATES),
        or_(
            and_(TestRun.last_heartbeat.is_not(None), heartbeat_deadline_expr < now),
            ttl_deadline_expr < now,
        ),
    )
    result = await db.execute(stmt)
    runs = result.scalars().all()

    for run in runs:
        if not (_heartbeat_stale(run, now) or _ttl_stale(run, now)):
            continue

        await assert_current_leader(db)
        # Re-check staleness under the row lock. The outer SELECT above
        # has no FOR UPDATE, so a concurrent ``heartbeat()`` could
        # refresh ``last_heartbeat`` between that snapshot and the lock
        # ``expire_run`` takes internally. Without this re-check the
        # reaper kills runs that just received a fresh heartbeat. The
        # WARN log is deferred until after the lock confirms the run is
        # still stale, so a near-miss does not produce a misleading
        # "Expiring run …" line for a run we ultimately leave alone.
        # The reason string is also picked from the condition still
        # stale under the lock — picking it from the pre-lock snapshot
        # could mislabel a TTL expiry as a heartbeat timeout (or vice
        # versa) when one predicate flipped between the SELECT and the
        # locked re-fetch.
        locked = await get_run_for_update(db, run.id)
        if locked is None:
            continue
        if locked.state in TERMINAL_STATES:
            await db.commit()
            continue
        current_now = datetime.now(UTC)
        heartbeat_stale = _heartbeat_stale(locked, current_now)
        ttl_stale = _ttl_stale(locked, current_now)
        if not (heartbeat_stale or ttl_stale):
            await db.commit()
            continue

        if heartbeat_stale:
            logger.warning(
                "Expiring run %s (%s): heartbeat timeout (last: %s, timeout: %ds)",
                locked.id,
                locked.name,
                locked.last_heartbeat,
                locked.heartbeat_timeout_sec,
            )
            await run_service.expire_run(db, locked, "Heartbeat timeout", publisher=publisher, settings=settings)
        else:
            logger.warning(
                "Expiring run %s (%s): TTL exceeded (%d minutes)",
                locked.id,
                locked.name,
                locked.ttl_minutes,
            )
            await run_service.expire_run(
                db, locked, f"TTL exceeded ({locked.ttl_minutes} minutes)", publisher=publisher, settings=settings
            )


async def run_reaper_loop(*, publisher: EventPublisher, settings: SettingsReader) -> None:
    """Background loop that expires stale test runs."""
    interval = float(settings.get("reservations.reaper_interval_sec"))
    # On startup, immediately check for stale runs (e.g. manager was restarted)
    try:
        async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
            await _reap_stale_runs(db, publisher=publisher, settings=settings)
    except LeadershipLost as exc:
        logger.error(
            "run_reaper_loop_leadership_lost",
            reason=str(exc),
            action="exiting_process_to_prevent_split_brain",
        )
        os._exit(70)
    except Exception:
        logger.exception("Initial stale run check failed")

    while True:
        await asyncio.sleep(interval)
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _reap_stale_runs(db, publisher=publisher, settings=settings)
        except LeadershipLost as exc:
            logger.error(
                "run_reaper_loop_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            logger.exception("Run reaper check failed")
