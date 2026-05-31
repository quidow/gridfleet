from app.devices.protocols import DeviceCapabilityProtocol
from app.devices.services.capability import DeviceCapabilityService


def test_capability_service_satisfies_protocol() -> None:
    assert isinstance(DeviceCapabilityService.__new__(DeviceCapabilityService), DeviceCapabilityProtocol)
