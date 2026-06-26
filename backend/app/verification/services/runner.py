from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx2 as httpx

from app.devices.schemas.device import DeviceVerificationCreate, DeviceVerificationUpdate
from app.jobs import JOB_KIND_DEVICE_VERIFICATION
from app.jobs.models import Job
from app.verification.services.job_state import finish_job, hydrate_job

if TYPE_CHECKING:
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher
    from app.verification.services.execution import VerificationExecutionService
    from app.verification.services.preparation import VerificationPreparationService

logger = logging.getLogger(__name__)


class VerificationRunnerService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        publisher: EventPublisher,
        settings: SettingsReader,
        circuit_breaker: CircuitBreakerProtocol,
        preparation: VerificationPreparationService,
        execution: VerificationExecutionService,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings
        self._circuit_breaker = circuit_breaker
        self._preparation = preparation
        self._execution = execution

    async def _load_persisted_job(
        self,
        job_id: str,
    ) -> dict[str, Any] | None:
        async with self._session_factory() as db:
            row = await db.get(Job, uuid.UUID(job_id))
        if row is None or row.kind != JOB_KIND_DEVICE_VERIFICATION:
            return None
        return hydrate_job(
            row.snapshot, db_job_id=job_id, session_factory=self._session_factory, publisher=self._publisher
        )

    async def run_persisted_verification_job(self, job_id: str, request: dict[str, Any]) -> None:
        job = await self._load_persisted_job(job_id)
        if job is None:
            return

        try:
            async with self._session_factory() as db:
                if request["mode"] == "create":
                    context, validation_error = await self._preparation.validate_create_request(
                        job,
                        db,
                        DeviceVerificationCreate.model_validate(request["data"]),
                        http_client_factory=httpx.AsyncClient,
                    )
                else:
                    context, validation_error = await self._preparation.validate_update_request(
                        job,
                        db,
                        uuid.UUID(str(request["device_id"])),
                        DeviceVerificationUpdate.model_validate(request["data"]),
                        http_client_factory=httpx.AsyncClient,
                    )

                if validation_error is not None or context is None:
                    await finish_job(job, status="failed", error=validation_error)
                    return

                outcome = await self._execution.execute_verification_context(
                    job,
                    db,
                    context,
                    http_client_factory=httpx.AsyncClient,
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
