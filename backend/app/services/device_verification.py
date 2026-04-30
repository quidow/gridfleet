import logging
import uuid
from typing import Any

import httpx

from app.database import async_session
from app.models.job import Job
from app.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.services import session_viability
from app.services.device_verification_execution import execute_verification_context
from app.services.device_verification_job_state import (
    finish_job,
    hydrate_job,
    new_job,
    public_snapshot,
)
from app.services.device_verification_preparation import validate_create_request, validate_update_request
from app.services.job_queue import JOB_KIND_DEVICE_VERIFICATION, create_job, delete_jobs_by_kind
from app.services.pack_platform_resolver import assert_runnable
from app.type_defs import SessionFactory

logger = logging.getLogger(__name__)


async def start_verification_job(
    data: DeviceVerificationCreate,
    session_factory: SessionFactory = async_session,
) -> dict[str, Any]:
    # Gate: ensure pack is runnable before creating verification job
    if data.pack_id is not None and data.platform_id is not None:
        async with session_factory() as db:
            await assert_runnable(db, pack_id=data.pack_id, platform_id=data.platform_id)

    job_uuid = uuid.uuid4()
    async with session_factory() as db:
        row = await create_job(
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
        row = await create_job(
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
        await delete_jobs_by_kind(db, kind=JOB_KIND_DEVICE_VERIFICATION)


async def store_verification_job_for_test(
    job_id: str,
    job: dict[str, Any],
    session_factory: SessionFactory = async_session,
) -> None:
    async with session_factory() as db:
        await create_job(
            db,
            kind=JOB_KIND_DEVICE_VERIFICATION,
            payload={"mode": "create", "data": {}},
            snapshot={**job, "job_id": job_id},
            max_attempts=1,
            job_id=uuid.UUID(job_id),
        )


async def run_persisted_verification_job(
    job_id: str,
    request: dict[str, Any],
    session_factory: SessionFactory,
) -> None:
    job = await _load_persisted_job(job_id, session_factory)
    if job is None:
        return

    try:
        async with session_factory() as db:
            if request["mode"] == "create":
                context, validation_error = await validate_create_request(
                    job,
                    db,
                    DeviceVerificationCreate.model_validate(request["data"]),
                    http_client_factory=httpx.AsyncClient,
                )
            else:
                context, validation_error = await validate_update_request(
                    job,
                    db,
                    uuid.UUID(str(request["device_id"])),
                    DeviceVerificationUpdate.model_validate(request["data"]),
                    http_client_factory=httpx.AsyncClient,
                )

            if validation_error is not None or context is None:
                await finish_job(job, status="failed", error=validation_error)
                return

            outcome = await execute_verification_context(
                job,
                db,
                context,
                http_client_factory=httpx.AsyncClient,
                probe_session_fn=lambda capabilities, timeout_sec: session_viability.probe_session_via_grid(
                    session_viability.build_probe_capabilities(capabilities),
                    timeout_sec,
                ),
            )
            await finish_job(
                job,
                status=outcome.status,
                error=outcome.error,
                device_id=outcome.device_id,
            )
    except Exception:
        logger.exception("Verification job %s crashed", job_id)
        await finish_job(job, status="failed", error="Verification job crashed unexpectedly")


async def _load_persisted_job(job_id: str, session_factory: SessionFactory) -> dict[str, Any] | None:
    async with session_factory() as db:
        row = await db.get(Job, uuid.UUID(job_id))
    if row is None or row.kind != JOB_KIND_DEVICE_VERIFICATION:
        return None
    return hydrate_job(row.snapshot, db_job_id=job_id, session_factory=session_factory)
