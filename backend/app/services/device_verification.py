import uuid
from typing import Any

from app.database import async_session
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs import queue as job_queue
from app.jobs.models import Job
from app.packs.services import platform_resolver as pack_platform_resolver
from app.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.services.device_verification_job_state import (
    new_job,
    public_snapshot,
)
from app.services.device_verification_runner import run_persisted_verification_job
from app.type_defs import SessionFactory

__all__ = [
    "run_persisted_verification_job",
]


async def start_verification_job(
    data: DeviceVerificationCreate,
    session_factory: SessionFactory = async_session,
) -> dict[str, Any]:
    # Gate: ensure pack is runnable before creating verification job
    if data.pack_id is not None and data.platform_id is not None:
        async with session_factory() as db:
            await pack_platform_resolver.assert_runnable(db, pack_id=data.pack_id, platform_id=data.platform_id)

    job_uuid = uuid.uuid4()
    async with session_factory() as db:
        row = await job_queue.create_job(
            db,
            kind=JOB_KIND_DEVICE_VERIFICATION,
            payload={"mode": "create", "data": data.model_dump(mode="json")},
            snapshot=new_job(str(job_uuid)),
            max_attempts=1,
            job_id=job_uuid,
        )
    return public_snapshot(row.snapshot)


async def start_existing_device_verification_job(
    device_id: uuid.UUID,
    data: DeviceVerificationUpdate,
    session_factory: SessionFactory = async_session,
) -> dict[str, Any]:
    job_uuid = uuid.uuid4()
    async with session_factory() as db:
        row = await job_queue.create_job(
            db,
            kind=JOB_KIND_DEVICE_VERIFICATION,
            payload={
                "mode": "update",
                "device_id": str(device_id),
                "data": data.model_dump(mode="json", exclude_unset=True),
            },
            snapshot=new_job(str(job_uuid)),
            max_attempts=1,
            job_id=job_uuid,
        )
    return public_snapshot(row.snapshot)


async def get_verification_job(
    job_id: str,
    session_factory: SessionFactory = async_session,
) -> dict[str, Any] | None:
    try:
        parsed_job_id = uuid.UUID(job_id)
    except ValueError:
        return None

    async with session_factory() as db:
        row = await db.get(Job, parsed_job_id)
    if row is None or row.kind != JOB_KIND_DEVICE_VERIFICATION:
        return None
    return public_snapshot(row.snapshot)


async def clear_verification_jobs(session_factory: SessionFactory = async_session) -> None:
    async with session_factory() as db:
        await job_queue.delete_jobs_by_kind(db, kind=JOB_KIND_DEVICE_VERIFICATION)


async def store_verification_job_for_test(
    job_id: str,
    job: dict[str, Any],
    session_factory: SessionFactory = async_session,
) -> None:
    async with session_factory() as db:
        await job_queue.create_job(
            db,
            kind=JOB_KIND_DEVICE_VERIFICATION,
            payload={"mode": "create", "data": {}},
            snapshot={**job, "job_id": job_id},
            max_attempts=1,
            job_id=uuid.UUID(job_id),
        )
