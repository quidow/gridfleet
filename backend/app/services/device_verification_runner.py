import logging
import uuid
from typing import Any

import httpx

from app.models.job import Job
from app.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.services import session_viability
from app.services.device_verification_execution import execute_verification_context
from app.services.device_verification_job_state import finish_job, hydrate_job
from app.services.device_verification_preparation import validate_create_request, validate_update_request
from app.services.job_kind_constants import JOB_KIND_DEVICE_VERIFICATION
from app.type_defs import SessionFactory

logger = logging.getLogger(__name__)


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
