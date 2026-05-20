import asyncio
import os
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import async_session
from app.core.leader.advisory import LeadershipLost, assert_current_leader
from app.core.observability import get_logger, observe_background_loop
from app.runs import service as run_service
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_reservation import get_run_for_update
from app.settings import settings_service

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


async def _reap_stale_runs(db: AsyncSession) -> None:
    now = datetime.now(UTC)

    stmt = select(TestRun).where(TestRun.state.in_(NON_TERMINAL_STATES))
    result = await db.execute(stmt)
    runs = result.scalars().all()

    for run in runs:
        if _heartbeat_stale(run, now):
            logger.warning(
                "Expiring run %s (%s): heartbeat timeout (last: %s, timeout: %ds)",
                run.id,
                run.name,
                run.last_heartbeat,
                run.heartbeat_timeout_sec,
            )
            await assert_current_leader(db)
            # Re-check staleness under the row lock. The outer SELECT above
            # has no FOR UPDATE, so a concurrent ``heartbeat()`` could
            # refresh ``last_heartbeat`` between that snapshot and the lock
            # ``expire_run`` takes internally. Without this re-check the
            # reaper kills runs that just received a fresh heartbeat.
            locked = await get_run_for_update(db, run.id)
            if locked is None:
                continue
            if locked.state in TERMINAL_STATES:
                await db.commit()
                continue
            current_now = datetime.now(UTC)
            if not (_heartbeat_stale(locked, current_now) or _ttl_stale(locked, current_now)):
                await db.commit()
                continue
            await run_service.expire_run(db, locked, "Heartbeat timeout")
            continue

        if _ttl_stale(run, now):
            logger.warning(
                "Expiring run %s (%s): TTL exceeded (%d minutes)",
                run.id,
                run.name,
                run.ttl_minutes,
            )
            await assert_current_leader(db)
            # See heartbeat-branch comment above for the rationale behind
            # the locked re-check.
            locked = await get_run_for_update(db, run.id)
            if locked is None:
                continue
            if locked.state in TERMINAL_STATES:
                await db.commit()
                continue
            current_now = datetime.now(UTC)
            if not (_heartbeat_stale(locked, current_now) or _ttl_stale(locked, current_now)):
                await db.commit()
                continue
            await run_service.expire_run(db, locked, f"TTL exceeded ({run.ttl_minutes} minutes)")
            continue


async def run_reaper_loop() -> None:
    """Background loop that expires stale test runs."""
    interval = float(settings_service.get("reservations.reaper_interval_sec"))
    # On startup, immediately check for stale runs (e.g. manager was restarted)
    try:
        async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
            await _reap_stale_runs(db)
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
                await _reap_stale_runs(db)
        except LeadershipLost as exc:
            logger.error(
                "run_reaper_loop_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            logger.exception("Run reaper check failed")
