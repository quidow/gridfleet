from app.portability.protocols import PortabilityExportProtocol
from app.portability.services.export import PortabilityExportService


def test_portability_export_service_satisfies_protocol() -> None:
    assert isinstance(PortabilityExportService.__new__(PortabilityExportService), PortabilityExportProtocol)
