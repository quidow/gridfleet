from app.devices.protocols import PortabilityExportProtocol
from app.devices.services.portability_export import PortabilityExportService


def test_portability_export_service_satisfies_protocol() -> None:
    assert isinstance(PortabilityExportService.__new__(PortabilityExportService), PortabilityExportProtocol)
