from app.appium_nodes.protocols import NodeHealthProtocol
from app.appium_nodes.services.node_health import NodeHealthService


def test_node_health_service_satisfies_protocol() -> None:
    assert isinstance(NodeHealthService.__new__(NodeHealthService), NodeHealthProtocol)
