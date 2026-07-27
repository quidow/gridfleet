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
) -> uuid.UUID | None:
    """Stage one durable remediation job; flush-only, so the caller owns the boundary.

    The partial unique index makes a second enqueue for the same
    ``(device, failure episode, action)`` a no-op while an earlier job is still
    pending or running, and ``RETURNING`` tells the caller which of the two
    happened. Nothing here commits: the job must live or die with the failure
    fact that justified it.
    """
    job_id = uuid.uuid4()
    # This insert takes Device before Job; the opposite order (Job before
    # Device) is taken in remediation_job._prepare -- see the note there for
    # why that inversion does not deadlock against ON CONFLICT DO NOTHING.
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
    await db.flush()
    return inserted_id
