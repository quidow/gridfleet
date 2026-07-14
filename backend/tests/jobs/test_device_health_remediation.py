from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.devices.models import Device
from app.devices.services.remediation import enqueue_device_health_remediation
from app.devices.services.remediation_job import RemediationJobService
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_COMPLETED
from tests.helpers import create_device

if TYPE_CHECKING:
    from app.hosts.models import Host


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


def test_device_health_remediation_schema_contract() -> None:
    assert JOB_KIND_DEVICE_HEALTH_REMEDIATION == "device_health_remediation"
    assert {
        "remediation_device_id",
        "failure_episode_id",
        "remediation_action_id",
    } <= {column.key for column in sa_inspect(Job).columns}
    assert "failure_episode_id" in {column.key for column in sa_inspect(Device).columns}


async def test_worker_self_cancels_when_device_healthy(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="healthy-remediation-device",
    )
    device.device_checks_healthy = True
    device.failure_episode_id = None
    await db_session.commit()

    old_episode_id = uuid.uuid4()
    job_id = await enqueue_device_health_remediation(
        db_session,
        device_id=device.id,
        failure_episode_id=old_episode_id,
        action_id="reconnect",
        commit=True,
    )
    assert job_id is not None

    dispatch = AsyncMock()
    with patch(
        "app.devices.services.remediation_job.link_repair.dispatch_recommended_action",
        new=dispatch,
    ):
        await RemediationJobService(
            session_factory=_session_factory(db_session),
            circuit_breaker=AsyncMock(),
            health=AsyncMock(),
        ).run_device_health_remediation_job(
            str(job_id),
            {
                "device_id": str(device.id),
                "failure_episode_id": str(old_episode_id),
                "action_id": "reconnect",
            },
        )

    dispatch.assert_not_awaited()
    db_session.expire_all()
    job = await db_session.get(Job, job_id)
    assert job is not None
    assert job.status == JOB_STATUS_COMPLETED
    assert any(word in str(job.snapshot.get("note", "")).lower() for word in ("recovered", "superseded"))
