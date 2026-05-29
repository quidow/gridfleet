from app.devices.protocols import DeviceStateWriter
from app.devices.services.state import DeviceStateService


def test_device_state_service_satisfies_protocol() -> None:
    assert isinstance(DeviceStateService.__new__(DeviceStateService), DeviceStateWriter)
