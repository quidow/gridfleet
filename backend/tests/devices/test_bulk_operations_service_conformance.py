from app.devices.protocols import BulkOperationsProtocol
from app.devices.services.bulk import BulkOperationsService


def test_bulk_operations_service_satisfies_protocol() -> None:
    assert isinstance(BulkOperationsService.__new__(BulkOperationsService), BulkOperationsProtocol)
