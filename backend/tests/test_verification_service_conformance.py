from app.verification.protocols import VerificationProtocol
from app.verification.services.service import VerificationService


def test_verification_service_satisfies_protocol() -> None:
    assert isinstance(VerificationService.__new__(VerificationService), VerificationProtocol)
