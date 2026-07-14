from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from app.devices.services import data_cleanup
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION, JOB_STATUS_COMPLETED
from app.jobs.models import Job
from tests.fakes import FakeSettingsReader
from tests.helpers import seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.db


async def test_terminal_remediation_job_is_retained_until_its_failure_episode_closes(
    db_session: AsyncSession,
) -> None:
    _host, device = await seed_host_and_device(db_session, identity="cleanup-remediation")
    failure_episode_id = uuid.uuid4()
    device.failure_episode_id = failure_episode_id
    old_time = datetime.now(UTC) - timedelta(days=90)
    job = Job(
        kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION,
        status=JOB_STATUS_COMPLETED,
        payload={
            "device_id": str(device.id),
            "failure_episode_id": str(failure_episode_id),
            "action_id": "reconnect",
        },
        remediation_device_id=device.id,
        failure_episode_id=failure_episode_id,
        remediation_action_id="reconnect",
        scheduled_at=old_time,
        completed_at=old_time,
        created_at=old_time,
        updated_at=old_time,
    )
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    service = data_cleanup.DataCleanupService(
        publisher=AsyncMock(),
        settings=FakeSettingsReader({"retention.test_runs_days": 0, "retention.jobs_days": 30}),
    )
    counts = data_cleanup._CleanupCounts()

    await service._cleanup_runs_and_jobs(db_session, datetime.now(UTC), counts)

    assert await db_session.get(Job, job_id) is not None
    assert counts.jobs_deleted == 0

    device.failure_episode_id = None
    await db_session.commit()

    await service._cleanup_runs_and_jobs(db_session, datetime.now(UTC), counts)

    assert await db_session.get(Job, job_id) is None
    assert counts.jobs_deleted == 1
