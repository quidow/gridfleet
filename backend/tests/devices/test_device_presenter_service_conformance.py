from app.devices.protocols import DevicePresenterProtocol
from app.devices.services.presenter import DevicePresenterService


def test_device_presenter_service_satisfies_protocol() -> None:
    assert isinstance(DevicePresenterService.__new__(DevicePresenterService), DevicePresenterProtocol)
