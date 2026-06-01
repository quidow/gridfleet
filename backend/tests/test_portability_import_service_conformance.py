from app.devices.protocols import PortabilityImportProtocol
from app.devices.services.portability_import import PortabilityImportService
from app.verification.services.service import VerificationService


def test_portability_import_service_satisfies_protocol() -> None:
    assert isinstance(PortabilityImportService(verification_enqueuer=VerificationService()), PortabilityImportProtocol)
