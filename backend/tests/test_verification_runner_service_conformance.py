from app.devices.services.verification_runner import VerificationRunnerService
from app.jobs.protocols import VerificationJobRunner


def test_verification_runner_service_satisfies_protocol() -> None:
    assert isinstance(VerificationRunnerService.__new__(VerificationRunnerService), VerificationJobRunner)
