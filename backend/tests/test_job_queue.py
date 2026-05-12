from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.job import Job
from app.services import job_queue


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


async def test_create_get_and_delete_jobs_by_kind(db_session: AsyncSession) -> None:
    job = await job_queue.create_job(
        db_session,
        kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
        payload={"device_id": "1"},
        snapshot={"status": job_queue.JOB_STATUS_PENDING},
    )

    loaded = await job_queue.get_job(db_session, job.id)
    assert loaded is not None
    assert loaded.payload == {"device_id": "1"}

    await job_queue.delete_jobs_by_kind(db_session, kind=job_queue.JOB_KIND_DEVICE_VERIFICATION)
    assert await job_queue.get_job(db_session, job.id) is None


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

    with patch(
        "app.services.job_queue.utcnow",
        return_value=datetime.now(UTC),
    ):
        with patch(
            "app.services.job_queue.reset_snapshot_for_retry",
            return_value={"status": job_queue.JOB_STATUS_PENDING, "retried": True},
        ):
            count_verification = await job_queue.reset_stale_running_jobs(
                _session_factory(db_session),
                kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
            )
        count_recovery = await job_queue.reset_stale_running_jobs(
            _session_factory(db_session),
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

    job = await job_queue.claim_next_job(_session_factory(db_session), kind=job_queue.JOB_KIND_DEVICE_RECOVERY)

    assert job is not None
    assert job.id == ready.id
    assert job.status == job_queue.JOB_STATUS_RUNNING
    assert await job_queue.claim_next_job(_session_factory(db_session), kind="missing") is None


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

    with patch(
        "app.services.job_queue.run_persisted_verification_job",
        new=AsyncMock(),
    ) as verification_runner:
        assert (
            await job_queue.run_pending_jobs_once(
                _session_factory(db_session),
                kind=job_queue.JOB_KIND_DEVICE_VERIFICATION,
            )
            is True
        )
    verification_runner.assert_awaited_once()

    with patch(
        "app.services.job_queue.run_device_recovery_job",
        new=AsyncMock(),
    ) as recovery_runner:
        assert (
            await job_queue.run_pending_jobs_once(
                _session_factory(db_session),
                kind=job_queue.JOB_KIND_DEVICE_RECOVERY,
            )
            is True
        )
    recovery_runner.assert_awaited_once()


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

    assert await job_queue.run_pending_jobs_once(_session_factory(db_session)) is True
    await db_session.refresh(unsupported)
    assert unsupported.status == job_queue.JOB_STATUS_FAILED
    assert "Unsupported job kind" in unsupported.snapshot["error"]


async def test_run_pending_jobs_once_returns_false_when_no_jobs(db_session: AsyncSession) -> None:
    assert await job_queue.run_pending_jobs_once(_session_factory(db_session)) is False


async def test_durable_job_worker_loop_handles_idle_and_error_cycles() -> None:
    session_factory = AsyncMock()

    class _Observation:
        @asynccontextmanager
        async def cycle(self) -> AsyncMock:
            yield session_factory

    with (
        patch("app.services.job_queue.observe_background_loop", return_value=_Observation()),
        patch("app.services.job_queue.reset_stale_running_jobs", new=AsyncMock()) as reset_jobs,
        patch(
            "app.services.job_queue.run_pending_jobs_once",
            new=AsyncMock(side_effect=[False, RuntimeError("boom"), asyncio.CancelledError()]),
        ),
        patch("app.services.job_queue.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await job_queue.durable_job_worker_loop(session_factory)

    assert reset_jobs.await_count == 2
    sleep.assert_awaited()
