from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs import queue as job_queue

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
@pytest.mark.db
async def test_create_job_with_commit_false_does_not_commit(db_session: AsyncSession) -> None:
    job_id = uuid.uuid4()
    job = await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_VERIFICATION,
        payload={"mode": "create"},
        snapshot={"job_id": str(job_id), "status": "pending"},
        max_attempts=1,
        job_id=job_id,
        commit=False,
    )
    assert job.id == job_id
    # Row is in the session but not committed: a rollback should remove it.
    await db_session.rollback()
    fetched = await db_session.get(job_queue.Job, job_id)
    assert fetched is None


@pytest.mark.asyncio
@pytest.mark.db
async def test_create_job_defaults_to_commit_true(db_session: AsyncSession) -> None:
    job_id = uuid.uuid4()
    job = await job_queue.create_job(
        db_session,
        kind=JOB_KIND_DEVICE_VERIFICATION,
        payload={"mode": "create"},
        snapshot={"job_id": str(job_id), "status": "pending"},
        max_attempts=1,
        job_id=job_id,
    )
    # Default commit=True: rollback does not undo the row.
    await db_session.rollback()
    fetched = await db_session.get(job_queue.Job, job_id)
    assert fetched is not None
    assert fetched.id == job.id
