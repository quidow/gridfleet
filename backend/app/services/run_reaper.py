import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models.test_run import TERMINAL_STATES, RunState, TestRun
from app.observability import get_logger, observe_background_loop
from app.services import run_service
from app.services.settings_service import settings_service

logger = get_logger(__name__)
LOOP_NAME = "run_reaper"

NON_TERMINAL_STATES = [s for s in RunState if s not in TERMINAL_STATES]


async def _reap_stale_runs(db: AsyncSession) -> None:
    now = datetime.now(UTC)

    stmt = select(TestRun).where(TestRun.state.in_(NON_TERMINAL_STATES))
    result = await db.execute(stmt)
    runs = result.scalars().all()

    for run in runs:
        # Check heartbeat timeout
        if run.last_heartbeat:
            deadline = run.last_heartbeat + timedelta(seconds=run.heartbeat_timeout_sec)
            if now > deadline:
                logger.warning(
                    "Expiring run %s (%s): heartbeat timeout (last: %s, timeout: %ds)",
                    run.id,
                    run.name,
                    run.last_heartbeat,
                    run.heartbeat_timeout_sec,
                )
                await run_service.expire_run(db, run, "Heartbeat timeout")
                continue

        # Check absolute TTL
        if run.created_at:
            ttl_deadline = run.created_at + timedelta(minutes=run.ttl_minutes)
            if now > ttl_deadline:
                logger.warning(
                    "Expiring run %s (%s): TTL exceeded (%d minutes)",
                    run.id,
                    run.name,
                    run.ttl_minutes,
                )
                await run_service.expire_run(db, run, f"TTL exceeded ({run.ttl_minutes} minutes)")
                continue


async def run_reaper_loop() -> None:
    """Background loop that expires stale test runs."""
    interval = float(settings_service.get("reservations.reaper_interval_sec"))
    # On startup, immediately check for stale runs (e.g. manager was restarted)
    try:
        async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
            await _reap_stale_runs(db)
    except Exception:
        logger.exception("Initial stale run check failed")

    while True:
        await asyncio.sleep(interval)
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _reap_stale_runs(db)
        except Exception:
            logger.exception("Run reaper check failed")
