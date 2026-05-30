"""Conformance test: RecoveryJobService implements RecoveryJobRunner protocol."""

from app.devices.services.recovery_job import RecoveryJobService
from app.jobs.protocols import RecoveryJobRunner


def test_recovery_job_service_satisfies_protocol() -> None:
    assert isinstance(RecoveryJobService.__new__(RecoveryJobService), RecoveryJobRunner)
