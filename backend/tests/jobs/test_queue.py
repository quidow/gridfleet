from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.recovery_job import RecoveryJobService
from app.devices.services.service import DeviceCrudService
from app.devices.services.verification_execution import VerificationExecutionService
from app.devices.services.verification_preparation import VerificationPreparationService
from app.devices.services.verification_runner import VerificationRunnerService
from app.jobs import queue as job_queue
from app.jobs.models import Job
from app.jobs.protocols import DurableJobProtocol
from app.jobs.queue import DurableJobService
from tests.fakes import FakeSettingsReader


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


def _make_service(db_session: AsyncSession) -> DurableJobService:
    sf = _session_factory(db_session)
    return DurableJobService(
        session_factory=sf,
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=AsyncMock(),
        verification_runner=VerificationRunnerService(
            session_factory=sf,
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=AsyncMock(),
            preparation=VerificationPreparationService(
                settings=FakeSettingsReader({}),
                circuit_breaker=AsyncMock(),
                crud=DeviceCrudService(settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService()),
                identity=DeviceIdentityConflictService(),
            ),
            execution=VerificationExecutionService(
                publisher=AsyncMock(),
                settings=FakeSettingsReader({}),
                circuit_breaker=AsyncMock(),
                crud=DeviceCrudService(settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService()),
                viability=Mock(),
                capability=DeviceCapabilityService(),
                reconciler=AsyncMock(),
                node_manager=AsyncMock(),
            ),
            viability=Mock(),
        ),
        recovery_runner=RecoveryJobService(
            session_factory=sf,
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            lifecycle_policy=AsyncMock(),
        ),
    )


async def test_create_and_delete_jobs_by_kind(db_session: AsyncSession) -> None:
    job = await job_queue.create_job(
        db_session,
        kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
        payload={"device_id": "1"},
        snapshot={"status": job_queue.JOB_STATUS_PENDING},
    )
    assert job.payload == {"device_id": "1"}

    await job_queue.delete_jobs_by_kind(db_session, kind=job_queue.JOB_KIND_DEVICE_VERIFICATION)
    loaded = await db_session.get(Job, job.id)
    assert loaded is None


async def test_reset_stale_running_jobs_handles_verification_and_other_kinds(db_session: AsyncSession) -> None:
    stale_started_at = datetime.now(UTC) - timedelta(minutes=20)
    verification = Job(
        id=uuid4(),
        kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
        status=job_queue.JOB_STATUS_RUNNING,
        payload={},
        snapshot={"status": job_queue.JOB_STATUS_RUNNING},
        started_at=stale_started_at,
        scheduled_at=datetime.now(UTC),
    )
    recovery = Job(
        id=uuid4(),
        kind=job_queue.JOB_KIND_DEVICE_RECOVERY,
        status=job_queue.JOB_STATUS_RUNNING,
        payload={},
        snapshot={"status": job_queue.JOB_STATUS_RUNNING, "error": "boom", "finished_at": "yesterday"},
        started_at=stale_started_at,
        scheduled_at=datetime.now(UTC),
    )
    db_session.add_all([verification, recovery])
    await db_session.commit()

    service = _make_service(db_session)
    with patch(
        "app.jobs.queue.utcnow",
        return_value=datetime.now(UTC),
    ):
        with patch(
            "app.jobs.queue.reset_snapshot_for_retry",
            return_value={"status": job_queue.JOB_STATUS_PENDING, "retried": True},
        ):
            count_verification = await service.reset_stale_running_jobs(
                kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
            )
        count_recovery = await service.reset_stale_running_jobs(
            kind=job_queue.JOB_KIND_DEVICE_RECOVERY,
        )

    await db_session.refresh(verification)
    await db_session.refresh(recovery)
    assert count_verification == 1
    assert count_recovery == 1
    assert verification.snapshot["retried"] is True
    assert recovery.snapshot["status"] == job_queue.JOB_STATUS_PENDING
    assert recovery.snapshot["error"] is None
    assert recovery.snapshot["finished_at"] is None


async def test_claim_next_job_respects_kind_and_schedule(db_session: AsyncSession) -> None:
    future = Job(
        id=uuid4(),
        kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
        status=job_queue.JOB_STATUS_PENDING,
        payload={},
        snapshot={},
        scheduled_at=datetime.now(UTC) + timedelta(hours=1),
    )
    ready = Job(
        id=uuid4(),
        kind=job_queue.JOB_KIND_DEVICE_RECOVERY,
        status=job_queue.JOB_STATUS_PENDING,
        payload={"device_id": "1"},
        snapshot={},
        scheduled_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add_all([future, ready])
    await db_session.commit()

    service = _make_service(db_session)
    job = await service.claim_next_job(kind=job_queue.JOB_KIND_DEVICE_RECOVERY)

    assert job is not None
    assert job.id == ready.id
    assert job.status == job_queue.JOB_STATUS_RUNNING
    assert await service.claim_next_job(kind="missing") is None


async def test_run_pending_jobs_once_dispatches_supported_kinds(db_session: AsyncSession) -> None:
    verification = Job(
        id=uuid4(),
        kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
        status=job_queue.JOB_STATUS_PENDING,
        payload={"device_id": "1"},
        snapshot={},
        scheduled_at=datetime.now(UTC),
    )
    recovery = Job(
        id=uuid4(),
        kind=job_queue.JOB_KIND_DEVICE_RECOVERY,
        status=job_queue.JOB_STATUS_PENDING,
        payload={"device_id": "1"},
        snapshot={},
        scheduled_at=datetime.now(UTC),
    )
    db_session.add_all([verification, recovery])
    await db_session.commit()

    sf = _session_factory(db_session)
    mock_verification_runner = AsyncMock()
    mock_verification_runner.run_persisted_verification_job = AsyncMock()
    mock_recovery_runner = AsyncMock()
    mock_recovery_runner.run_device_recovery_job = AsyncMock()
    service = DurableJobService(
        session_factory=sf,
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=AsyncMock(),
        verification_runner=mock_verification_runner,
        recovery_runner=mock_recovery_runner,
    )
    assert await service.run_pending_once(kind=job_queue.JOB_KIND_DEVICE_VERIFICATION) is True
    mock_verification_runner.run_persisted_verification_job.assert_awaited_once()

    assert await service.run_pending_once(kind=job_queue.JOB_KIND_DEVICE_RECOVERY) is True
    mock_recovery_runner.run_device_recovery_job.assert_awaited_once()


async def test_run_pending_jobs_once_marks_unsupported_job_failed(db_session: AsyncSession) -> None:
    unsupported = Job(
        id=uuid4(),
        kind="mystery",
        status=job_queue.JOB_STATUS_PENDING,
        payload={"x": 1},
        snapshot={"status": job_queue.JOB_STATUS_PENDING},
        scheduled_at=datetime.now(UTC),
    )
    db_session.add(unsupported)
    await db_session.commit()

    service = _make_service(db_session)
    result = await service.run_pending_once()
    assert result is True
    await db_session.refresh(unsupported)
    assert unsupported.status == job_queue.JOB_STATUS_FAILED
    assert "Unsupported job kind" in unsupported.snapshot["error"]


async def test_run_pending_jobs_once_returns_false_when_no_jobs(db_session: AsyncSession) -> None:
    service = _make_service(db_session)
    result = await service.run_pending_once()
    assert result is False


async def test_durable_job_worker_loop_handles_idle_and_error_cycles() -> None:
    class _Observation:
        @asynccontextmanager
        async def cycle(self) -> AsyncMock:
            yield AsyncMock()

    mock_service = AsyncMock(spec=DurableJobService)
    mock_service.reset_stale_running_jobs = AsyncMock(return_value=0)
    mock_service.run_pending_once = AsyncMock(side_effect=[False, RuntimeError("boom"), asyncio.CancelledError()])

    with (
        patch("app.jobs.queue.observe_background_loop", return_value=_Observation()),
        patch("app.jobs.queue.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        loop = job_queue.DurableJobWorkerLoop(service=mock_service)
        await loop.run()

    assert mock_service.reset_stale_running_jobs.await_count == 2
    sleep.assert_awaited()


def test_durable_job_service_satisfies_protocol() -> None:
    assert issubclass(DurableJobService, DurableJobProtocol)
