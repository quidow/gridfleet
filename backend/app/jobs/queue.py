from __future__ import annotations

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select

from app.core.metrics import register_gauge_refresher
from app.jobs.kinds import JOB_KIND_DEVICE_RECOVERY, JOB_KIND_DEVICE_VERIFICATION
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_FAILED, JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from app.metrics_recorders import PENDING_JOBS
from app.observability import get_logger, observe_background_loop
from app.services.device_recovery_job import run_device_recovery_job
from app.services.device_verification_job_state import reset_snapshot_for_retry
from app.services.device_verification_runner import run_persisted_verification_job

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = get_logger(__name__)
JOB_POLL_INTERVAL_SEC = 1
STALE_JOB_TIMEOUT = timedelta(minutes=10)
LOOP_NAME = "durable_job_worker"


async def _refresh_jobs_gauges(db: AsyncSession) -> None:
    pending_jobs_result = await db.execute(
        select(func.count()).select_from(Job).where(Job.status == JOB_STATUS_PENDING)
    )
    PENDING_JOBS.set(int(pending_jobs_result.scalar_one()))


register_gauge_refresher(_refresh_jobs_gauges)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def create_job(
    db: AsyncSession,
    *,
    kind: str,
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    max_attempts: int = 1,
    scheduled_at: datetime | None = None,
    job_id: uuid.UUID | None = None,
) -> Job:
    job = Job(
        id=job_id or uuid.uuid4(),
        kind=kind,
        status=str(snapshot.get("status") or JOB_STATUS_PENDING),
        payload=copy.deepcopy(payload),
        snapshot=copy.deepcopy(snapshot),
        max_attempts=max_attempts,
        scheduled_at=scheduled_at or utcnow(),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    return job


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> Job | None:
    return await db.get(Job, job_id)


async def delete_jobs_by_kind(db: AsyncSession, *, kind: str) -> None:
    result = await db.execute(select(Job).where(Job.kind == kind))
    for row in result.scalars().all():
        await db.delete(row)
    await db.commit()


async def reset_stale_running_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str = JOB_KIND_DEVICE_VERIFICATION,
    timeout: timedelta = STALE_JOB_TIMEOUT,
) -> int:
    cutoff = utcnow() - timeout
    async with session_factory() as db:
        result = await db.execute(
            select(Job).where(
                Job.kind == kind,
                Job.status == JOB_STATUS_RUNNING,
                Job.started_at.is_not(None),
                Job.started_at < cutoff,
            )
        )
        rows = result.scalars().all()
        for row in rows:
            row.status = JOB_STATUS_PENDING
            row.started_at = None
            row.completed_at = None
            if row.kind == JOB_KIND_DEVICE_VERIFICATION:
                row.snapshot = reset_snapshot_for_retry(row.snapshot)
            else:
                snapshot = copy.deepcopy(row.snapshot)
                snapshot["status"] = JOB_STATUS_PENDING
                snapshot["error"] = None
                snapshot["finished_at"] = None
                row.snapshot = snapshot
        await db.commit()
    if rows:
        logger.warning("Reset %d stale %s jobs back to pending", len(rows), kind)
    return len(rows)


async def claim_next_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str | None = None,
) -> Job | None:
    async with session_factory() as db:
        stmt = (
            select(Job)
            .where(
                Job.status == JOB_STATUS_PENDING,
                or_(Job.scheduled_at.is_(None), Job.scheduled_at <= utcnow()),
            )
            .order_by(Job.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if kind is not None:
            stmt = stmt.where(Job.kind == kind)
        result = await db.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            await db.rollback()
            return None

        row.status = JOB_STATUS_RUNNING
        row.attempts += 1
        row.started_at = utcnow()
        row.completed_at = None
        await db.commit()
        await db.refresh(row)
        return row


async def run_pending_jobs_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    kind: str | None = None,
) -> bool:
    row = await claim_next_job(session_factory, kind=kind)
    if row is None:
        return False

    if row.kind == JOB_KIND_DEVICE_VERIFICATION:
        await run_persisted_verification_job(
            str(row.id),
            row.payload,
            session_factory=session_factory,
        )
        return True

    if row.kind == JOB_KIND_DEVICE_RECOVERY:
        await run_device_recovery_job(
            str(row.id),
            row.payload,
            session_factory=session_factory,
        )
        return True

    async with session_factory() as db:
        job = await db.get(Job, row.id)
        if job is None:
            return True
        job.status = JOB_STATUS_FAILED
        snapshot = copy.deepcopy(job.snapshot)
        snapshot["status"] = JOB_STATUS_FAILED
        snapshot["error"] = f"Unsupported job kind: {row.kind}"
        snapshot["finished_at"] = utcnow().isoformat()
        job.snapshot = snapshot
        job.completed_at = utcnow()
        await db.commit()
    return True


async def durable_job_worker_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with observe_background_loop(LOOP_NAME, float(JOB_POLL_INTERVAL_SEC)).cycle():
        await reset_stale_running_jobs(session_factory)
        await reset_stale_running_jobs(session_factory, kind=JOB_KIND_DEVICE_RECOVERY)
    while True:
        try:
            async with observe_background_loop(LOOP_NAME, float(JOB_POLL_INTERVAL_SEC)).cycle():
                worked = await run_pending_jobs_once(session_factory)
            if not worked:
                await asyncio.sleep(JOB_POLL_INTERVAL_SEC)
        except Exception:
            logger.exception("Durable job worker error")
            await asyncio.sleep(JOB_POLL_INTERVAL_SEC)
