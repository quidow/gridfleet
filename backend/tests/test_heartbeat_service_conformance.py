from app.appium_nodes.protocols import HeartbeatProtocol
from app.appium_nodes.services.heartbeat import HeartbeatService


def test_heartbeat_service_satisfies_protocol() -> None:
    assert isinstance(HeartbeatService.__new__(HeartbeatService), HeartbeatProtocol)
