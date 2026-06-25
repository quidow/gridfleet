from app.appium_nodes.protocols import DeviceNodeHealthWriter
from app.devices.protocols import DeviceHealthProtocol
from app.devices.services.health import DeviceHealthService
from app.sessions.protocols import DeviceSessionViabilityWriter


def test_device_health_service_satisfies_protocol() -> None:
    svc = DeviceHealthService.__new__(DeviceHealthService)
    assert isinstance(svc, DeviceHealthProtocol)


def test_device_health_service_satisfies_node_health_writer() -> None:
    svc = DeviceHealthService.__new__(DeviceHealthService)
    assert isinstance(svc, DeviceNodeHealthWriter)


def test_device_health_service_satisfies_session_viability_writer() -> None:
    svc = DeviceHealthService.__new__(DeviceHealthService)
    assert isinstance(svc, DeviceSessionViabilityWriter)
