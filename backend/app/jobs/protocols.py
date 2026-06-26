"""Jobs domain Protocol definitions."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from app.jobs.kinds import JOB_KIND_DEVICE_VERIFICATION

if TYPE_CHECKING:
    from typing import Any

    from app.jobs.models import Job

STALE_JOB_TIMEOUT = timedelta(minutes=10)


class DurableJobProtocol(Protocol):
    async def reset_stale_running_jobs(
        self, *, kind: str = JOB_KIND_DEVICE_VERIFICATION, timeout: timedelta = STALE_JOB_TIMEOUT
    ) -> int: ...
    async def claim_next_job(self, *, kind: str | None = None) -> Job | None: ...
    async def run_pending_once(self, *, kind: str | None = None) -> bool: ...


class VerificationJobRunner(Protocol):
    async def run_persisted_verification_job(self, job_id: str, request: dict[str, Any]) -> None: ...


class RecoveryJobRunner(Protocol):
    async def run_device_recovery_job(self, job_id: str, payload: dict[str, Any]) -> None: ...
