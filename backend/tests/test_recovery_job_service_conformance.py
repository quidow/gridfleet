"""Conformance test: RecoveryJobService implements RecoveryJobRunner protocol."""

from app.jobs.protocols import RecoveryJobRunner
from app.lifecycle.services.recovery_job import RecoveryJobService


def test_recovery_job_service_satisfies_protocol() -> None:
    assert isinstance(RecoveryJobService.__new__(RecoveryJobService), RecoveryJobRunner)
