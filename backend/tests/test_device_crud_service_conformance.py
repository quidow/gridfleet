from app.devices.protocols import DeviceCrudProtocol
from app.devices.services.service import DeviceCrudService


def test_device_crud_service_satisfies_protocol() -> None:
    assert isinstance(DeviceCrudService.__new__(DeviceCrudService), DeviceCrudProtocol)
