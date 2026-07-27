from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select, update

from app.core.background_loop import BackgroundLoop
from app.core.db_retry import retry_on_serialization_failure
from app.core.metrics import register_gauge_refresher
from app.core.metrics_recorders import PENDING_JOBS
from app.core.observability import get_logger, observe_background_loop
from app.core.timeutil import now_utc
from app.jobs.kinds import (
    JOB_KIND_DEVICE_HEALTH_REMEDIATION,
    JOB_KIND_DEVICE_RECOVERY,
    JOB_KIND_DEVICE_VERIFICATION,
    JOB_KIND_RUN_SESSION_TEARDOWN,
    JOB_KIND_SESSION_KILL,
)
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_FAILED, JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from app.verification.services.job_state import reset_snapshot_for_retry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher
    from app.jobs.protocols import (
        RecoveryJobRunner,
        RemediationJobRunner,
        RunTeardownJobRunner,
        SessionKillJobRunner,
        VerificationJobRunner,
    )

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


@dataclass(frozen=True, slots=True)
class JobClaim:
    id: uuid.UUID
    kind: str
    payload: dict[str, Any]
    attempts: int


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
        scheduled_at=scheduled_at or now_utc(),
    )
    db.add(job)
    await db.flush()
    return job


class DurableJobService:
    def __init__(  # noqa: PLR0913 - one collaborator per dispatchable job kind
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        verification_runner: VerificationJobRunner,
        recovery_runner: RecoveryJobRunner,
        remediation_runner: RemediationJobRunner,
        run_teardown_runner: RunTeardownJobRunner,
        session_kill_runner: SessionKillJobRunner,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._verification_runner = verification_runner
        self._recovery_runner = recovery_runner
        self._remediation_runner = remediation_runner
        self._run_teardown_runner = run_teardown_runner
        self._session_kill_runner = session_kill_runner

    async def reset_stale_running_jobs(
        self,
        *,
        kind: str = JOB_KIND_DEVICE_VERIFICATION,
        timeout: timedelta = STALE_JOB_TIMEOUT,
    ) -> int:
        cutoff = now_utc() - timeout
        async with self._session_factory.begin() as db:
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
        if rows:
            logger.warning("Reset %d stale %s jobs back to pending", len(rows), kind)
        return len(rows)

    async def claim_next_job(self, *, kind: str | None = None) -> JobClaim | None:
        async def _attempt(db: AsyncSession) -> JobClaim | None:
            claimed_at = now_utc()
            candidate_id = (
                select(Job.id)
                .where(
                    Job.status == JOB_STATUS_PENDING,
                    or_(Job.scheduled_at.is_(None), Job.scheduled_at <= claimed_at),
                    *([Job.kind == kind] if kind is not None else []),
                )
                .order_by(Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
                .scalar_subquery()
            )
            row = (
                await db.execute(
                    update(Job)
                    .where(Job.id == candidate_id, Job.status == JOB_STATUS_PENDING)
                    .values(
                        status=JOB_STATUS_RUNNING,
                        attempts=Job.attempts + 1,
                        started_at=claimed_at,
                        completed_at=None,
                    )
                    .returning(Job.id, Job.kind, Job.payload, Job.attempts)
                    .execution_options(synchronize_session=False)
                )
            ).one_or_none()
            return None if row is None else JobClaim(row.id, row.kind, copy.deepcopy(row.payload), row.attempts)

        return await retry_on_serialization_failure(self._session_factory, _attempt, caller="job_claim")

    async def run_pending_once(self, *, kind: str | None = None) -> bool:  # noqa: PLR0911 - one dispatch per job kind
        claim = await self.claim_next_job(kind=kind)
        if claim is None:
            return False

        if claim.kind == JOB_KIND_DEVICE_VERIFICATION:
            await self._verification_runner.run_persisted_verification_job(str(claim.id), claim.payload)
            return True

        if claim.kind == JOB_KIND_DEVICE_RECOVERY:
            await self._recovery_runner.run_device_recovery_job(str(claim.id), claim.payload)
            return True

        if claim.kind == JOB_KIND_DEVICE_HEALTH_REMEDIATION:
            await self._remediation_runner.run_device_health_remediation_job(str(claim.id), claim.payload)
            return True

        if claim.kind == JOB_KIND_RUN_SESSION_TEARDOWN:
            await self._run_teardown_runner.run_run_session_teardown_job(str(claim.id), claim.payload)
            return True

        if claim.kind == JOB_KIND_SESSION_KILL:
            await self._session_kill_runner.run_session_kill_job(str(claim.id), claim.payload)
            return True

        async with self._session_factory.begin() as db:
            job = await db.get(Job, claim.id)
            if job is None:
                return True
            job.status = JOB_STATUS_FAILED
            snapshot = copy.deepcopy(job.snapshot)
            snapshot["status"] = JOB_STATUS_FAILED
            snapshot["error"] = f"Unsupported job kind: {claim.kind}"
            snapshot["finished_at"] = now_utc().isoformat()
            job.snapshot = snapshot
            job.completed_at = now_utc()
        return True


class DurableJobWorkerLoop(BackgroundLoop):
    """Job-queue poller on the shared loop skeleton.

    ``_wait`` drains back-to-back: it skips the poll sleep while the previous
    cycle found work, so a burst of queued jobs is not throttled to one per
    poll interval.
    """

    loop_name = LOOP_NAME
    cycle_failed_message = "Durable job worker error"

    def __init__(self, *, service: DurableJobService, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._service = service
        self._sf = session_factory
        self._worked = False

    @property
    def _session_factory(self) -> SessionFactory:
        return self._sf

    def _interval(self) -> float:
        return float(JOB_POLL_INTERVAL_SEC)

    async def _on_start(self) -> None:
        async with observe_background_loop(LOOP_NAME, float(JOB_POLL_INTERVAL_SEC)).cycle():
            await self._service.reset_stale_running_jobs()
            await self._service.reset_stale_running_jobs(kind=JOB_KIND_DEVICE_RECOVERY)
            await self._service.reset_stale_running_jobs(kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION)
            await self._service.reset_stale_running_jobs(kind=JOB_KIND_RUN_SESSION_TEARDOWN)
            await self._service.reset_stale_running_jobs(kind=JOB_KIND_SESSION_KILL)

    async def _run_cycle(self, db: AsyncSession) -> None:
        del db  # DurableJobService opens its own session per claim/run
        self._worked = False  # reset first so a raising cycle still sleeps in _wait
        self._worked = await self._service.run_pending_once()

    async def _wait(self, interval: float) -> None:
        if not self._worked:
            await asyncio.sleep(interval)
