from app.devices.protocols import PropertyRefreshProtocol
from app.devices.services.property_refresh import PropertyRefreshService


def test_property_refresh_service_satisfies_protocol() -> None:
    assert isinstance(PropertyRefreshService.__new__(PropertyRefreshService), PropertyRefreshProtocol)
