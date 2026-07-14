from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.services.capability import DeviceCapabilityService
from app.devices.services.identity_conflicts import DeviceIdentityConflictService
from app.devices.services.service import DeviceCrudService
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs import queue as job_queue
from app.jobs.models import Job
from app.jobs.queue import DurableJobService
from app.lifecycle.services.recovery_job import RecoveryJobService
from app.verification.services.execution import AgentCallContext, VerificationExecutionService
from app.verification.services.preparation import VerificationPreparationService
from app.verification.services.runner import VerificationRunnerService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus


def _fake_session_factory() -> Mock:
    factory = Mock()
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=AsyncMock())
    cm.__aexit__ = AsyncMock(return_value=False)
    factory.return_value = cm
    return factory


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
        remediation_runner=AsyncMock(),
        verification_runner=VerificationRunnerService(
            session_factory=sf,
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=AsyncMock(),
            preparation=VerificationPreparationService(
                settings=FakeSettingsReader({}),
                circuit_breaker=AsyncMock(),
                crud=DeviceCrudService(
                    settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                identity=DeviceIdentityConflictService(),
            ),
            execution=VerificationExecutionService(
                review=build_review_service(),
                publisher=AsyncMock(),
                agent=AgentCallContext(settings=FakeSettingsReader({}), circuit_breaker=AsyncMock()),
                crud=DeviceCrudService(
                    settings=FakeSettingsReader({}), identity=DeviceIdentityConflictService(), publisher=event_bus
                ),
                viability=Mock(),
                capability=DeviceCapabilityService(),
                reconciler=AsyncMock(),
                node_manager=AsyncMock(),
            ),
        ),
        recovery_runner=RecoveryJobService(
            session_factory=sf,
            publisher=AsyncMock(),
            settings=FakeSettingsReader({}),
            lifecycle_policy=AsyncMock(),
        ),
    )


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
        "app.jobs.queue.now_utc",
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
        remediation_runner=AsyncMock(),
    )
    assert await service.run_pending_once(kind=job_queue.JOB_KIND_DEVICE_VERIFICATION) is True
    mock_verification_runner.run_persisted_verification_job.assert_awaited_once()

    assert await service.run_pending_once(kind=job_queue.JOB_KIND_DEVICE_RECOVERY) is True
    mock_recovery_runner.run_device_recovery_job.assert_awaited_once()


async def test_run_pending_jobs_once_dispatches_remediation_kind(db_session: AsyncSession) -> None:
    remediation = Job(
        id=uuid4(),
        kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION,
        status=job_queue.JOB_STATUS_PENDING,
        payload={"device_id": "1", "failure_episode_id": "2", "action_id": "reconnect"},
        snapshot={},
        scheduled_at=datetime.now(UTC),
    )
    db_session.add(remediation)
    await db_session.commit()

    remediation_runner = AsyncMock()
    remediation_runner.run_device_health_remediation_job = AsyncMock()
    service = DurableJobService(
        session_factory=_session_factory(db_session),
        publisher=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=AsyncMock(),
        verification_runner=AsyncMock(),
        recovery_runner=AsyncMock(),
        remediation_runner=remediation_runner,
    )

    assert await service.run_pending_once(kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION) is True
    remediation_runner.run_device_health_remediation_job.assert_awaited_once_with(
        str(remediation.id),
        remediation.payload,
    )


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


async def test_durable_job_worker_loop_wait_and_error_semantics() -> None:
    mock_service = Mock()
    mock_service.reset_stale_running_jobs = AsyncMock(return_value=0)
    mock_service.run_pending_once = AsyncMock(side_effect=[True, RuntimeError("boom"), False])
    loop = job_queue.DurableJobWorkerLoop(service=mock_service, session_factory=_fake_session_factory())

    await loop._on_start()
    assert mock_service.reset_stale_running_jobs.await_count == 3
    mock_service.reset_stale_running_jobs.assert_any_await(kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION)

    # worked=True → no sleep between cycles
    await loop._run_cycle(AsyncMock())
    with patch("app.jobs.queue.asyncio.sleep", new=AsyncMock()) as sleep:
        await loop._wait(1.0)
    sleep.assert_not_awaited()

    # a raising cycle must still sleep (worked resets to False before the call)
    with pytest.raises(RuntimeError):
        await loop._run_cycle(AsyncMock())
    with patch("app.jobs.queue.asyncio.sleep", new=AsyncMock()) as sleep:
        await loop._wait(1.0)
    sleep.assert_awaited_once_with(1.0)
