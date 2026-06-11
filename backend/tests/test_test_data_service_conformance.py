# Imported under a non-``Test*`` alias so pytest does not try to collect the
# Protocol as a test class (it is a runtime_checkable Protocol, so ``__test__``
# cannot be set on it without altering its structural member set).
from app.devices.protocols import TestDataProtocol as DataProtocol
from app.devices.services.test_data import TestDataService


def test_test_data_service_satisfies_protocol() -> None:
    assert isinstance(TestDataService.__new__(TestDataService), DataProtocol)
