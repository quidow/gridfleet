"""Phase 11 boundary regressions for the recovery-job runner.

Each phase owns one explicit transaction; a phase that raises must leave no
partial write, and the lock-failure path must finalize the job on a session the
failure did not abort.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from sqlalchemy import text

from app.jobs import JOB_STATUS_FAILED, JOB_STATUS_PENDING
from app.jobs.models import Job
from app.lifecycle.services.recovery_job import RecoveryJobService
from tests.fakes import FakeSettingsReader
from tests.fakes.session_factory import RecordingSessionFactory

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _service(session_factory: async_sessionmaker[AsyncSession]) -> RecoveryJobService:
    return RecoveryJobService(
        session_factory=session_factory,
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        lifecycle_policy=AsyncMock(),
        viability=AsyncMock(),
    )


async def test_lock_failure_finalizes_the_job_on_a_session_the_failure_did_not_abort(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: object,
) -> None:
    """A REAL statement failure while locking must still mark the job failed.

    The failure is injected as a genuine bad statement, not a patched method:
    a synthetic raise leaves the session clean, while a real error aborts the
    transaction — and writing the job row on an aborted transaction is exactly
    the bug this pins.
    """
    job_id = uuid.uuid4()
    db_session.add(Job(id=job_id, kind="device_recovery", status=JOB_STATUS_PENDING, payload={}, snapshot={}))
    await db_session.commit()

    async def _fail_the_lock(session: AsyncSession, statement: str) -> None:
        if "for update" in statement and " devices" in statement:
            await session.execute(text("SELECT no_such_function_phase11()"))

    recorder = RecordingSessionFactory(db_session_maker)
    recorder.hook = _fail_the_lock
    service = _service(recorder)  # type: ignore[arg-type]

    await service.run_device_recovery_job(str(job_id), {"device_id": str(uuid.uuid4())})

    db_session.expire_all()
    row = await db_session.get(Job, job_id)
    assert row is not None
    assert row.status == JOB_STATUS_FAILED, "a real lock failure must still finalize the job row"
    assert "could not be locked" in (row.snapshot.get("error") or "")
