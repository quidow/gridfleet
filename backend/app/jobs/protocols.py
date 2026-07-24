"""Jobs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from typing import Any


class VerificationJobRunner(Protocol):
    async def run_persisted_verification_job(self, job_id: str, request: dict[str, Any]) -> None: ...


class RecoveryJobRunner(Protocol):
    async def run_device_recovery_job(self, job_id: str, payload: dict[str, Any]) -> None: ...


class RemediationJobRunner(Protocol):
    async def run_device_health_remediation_job(self, job_id: str, payload: dict[str, Any]) -> None: ...


class RunTeardownJobRunner(Protocol):
    async def run_run_session_teardown_job(self, job_id: str, payload: dict[str, Any]) -> None: ...


class SessionKillJobRunner(Protocol):
    async def run_session_kill_job(self, job_id: str, payload: dict[str, Any]) -> None: ...
