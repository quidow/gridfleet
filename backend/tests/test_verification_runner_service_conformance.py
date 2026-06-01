from app.jobs.protocols import VerificationJobRunner
from app.verification.services.runner import VerificationRunnerService


def test_verification_runner_service_satisfies_protocol() -> None:
    assert isinstance(VerificationRunnerService.__new__(VerificationRunnerService), VerificationJobRunner)
