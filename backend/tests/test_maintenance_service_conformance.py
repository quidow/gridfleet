from app.devices.protocols import MaintenanceProtocol
from app.devices.services.maintenance import MaintenanceService


def test_maintenance_service_satisfies_protocol() -> None:
    assert isinstance(MaintenanceService.__new__(MaintenanceService), MaintenanceProtocol)
