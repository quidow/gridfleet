from app.devices.protocols import ConnectivityProtocol
from app.devices.services.connectivity import ConnectivityService


def test_connectivity_service_satisfies_protocol() -> None:
    assert isinstance(ConnectivityService.__new__(ConnectivityService), ConnectivityProtocol)
