from app.devices.protocols import TestDataProtocol
from app.devices.services.test_data import TestDataService


def test_test_data_service_satisfies_protocol() -> None:
    assert isinstance(TestDataService.__new__(TestDataService), TestDataProtocol)
