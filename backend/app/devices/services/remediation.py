from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_PENDING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def enqueue_device_health_remediation(
    db: AsyncSession,
    *,
    device_id: uuid.UUID,
    failure_episode_id: uuid.UUID,
    action_id: str,
    commit: bool = False,
) -> uuid.UUID | None:
    job_id = uuid.uuid4()
    stmt = (
        pg_insert(Job)
        .values(
            id=job_id,
            kind=JOB_KIND_DEVICE_HEALTH_REMEDIATION,
            status=JOB_STATUS_PENDING,
            payload={
                "device_id": str(device_id),
                "failure_episode_id": str(failure_episode_id),
                "action_id": action_id,
            },
            snapshot={"status": JOB_STATUS_PENDING},
            max_attempts=1,
            remediation_device_id=device_id,
            failure_episode_id=failure_episode_id,
            remediation_action_id=action_id,
        )
        .on_conflict_do_nothing(
            index_elements=[
                "remediation_device_id",
                "failure_episode_id",
                "remediation_action_id",
            ],
            index_where=text("status IN ('pending', 'running') AND remediation_device_id IS NOT NULL"),
        )
        .returning(Job.id)
    )
    inserted_id = (await db.execute(stmt)).scalar_one_or_none()
    if commit:
        await db.commit()
    else:
        await db.flush()
    return inserted_id
