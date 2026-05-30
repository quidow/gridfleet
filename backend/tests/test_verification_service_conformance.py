from app.devices.protocols import VerificationProtocol
from app.devices.services.verification import VerificationService


def test_verification_service_satisfies_protocol() -> None:
    assert isinstance(VerificationService.__new__(VerificationService), VerificationProtocol)
