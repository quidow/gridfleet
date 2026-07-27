import inspect
import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.services.remediation import enqueue_device_health_remediation
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from tests.helpers import seed_host_and_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class EnqueueRollbackProbeError(Exception):
    pass


def test_enqueue_device_health_remediation_declares_no_commit_ownership() -> None:
    parameters = inspect.signature(enqueue_device_health_remediation).parameters
    assert "commit" not in parameters
    assert "autocommit" not in parameters
    assert "rollback" not in parameters


async def test_enqueue_device_health_remediation_is_flush_only(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="remediation-enqueue-flush-only")
    device_id = device.id
    await db_session.commit()
    episode_id = uuid.uuid4()

    with pytest.raises(EnqueueRollbackProbeError):
        async with db_session.begin():
            job_id = await enqueue_device_health_remediation(
                db_session,
                device_id=device_id,
                failure_episode_id=episode_id,
                action_id="reconnect",
            )
            assert job_id is not None
            raise EnqueueRollbackProbeError

    assert (await db_session.execute(select(Job).where(Job.remediation_device_id == device_id))).first() is None


async def test_enqueue_device_health_remediation_deduplicates_active_episode_action(
    db_session: AsyncSession,
) -> None:
    _host, device = await seed_host_and_device(db_session, identity="remediation-enqueue")
    first_episode_id = uuid.uuid4()

    first_job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=first_episode_id,
        action_id="reconnect",
    )
    duplicate_job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=first_episode_id,
        action_id="reconnect",
    )

    assert isinstance(first_job_id, uuid.UUID)
    assert duplicate_job_id is None
    active_jobs = (
        (
            await db_session.execute(
                select(Job).where(
                    Job.remediation_device_id == device.id,
                    Job.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING)),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(active_jobs) == 1

    second_job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=uuid.uuid4(),
        action_id="reconnect",
    )

    assert isinstance(second_job_id, uuid.UUID)
    first_job = await db_session.get(Job, first_job_id)
    assert first_job is not None
    assert first_job.kind == JOB_KIND_DEVICE_HEALTH_REMEDIATION
    assert first_job.payload["action_id"] == "reconnect"
