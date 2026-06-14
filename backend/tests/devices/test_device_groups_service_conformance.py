from app.devices.protocols import DeviceGroupsProtocol
from app.devices.services.groups import DeviceGroupsService


def test_device_groups_service_satisfies_protocol() -> None:
    assert isinstance(DeviceGroupsService.__new__(DeviceGroupsService), DeviceGroupsProtocol)
