from app.devices.protocols import DataCleanupProtocol
from app.devices.services.data_cleanup import DataCleanupService


def test_data_cleanup_service_satisfies_protocol() -> None:
    assert isinstance(DataCleanupService.__new__(DataCleanupService), DataCleanupProtocol)
