from app.appium_nodes.protocols import OperatorNodeManager
from app.devices.protocols import OperatorNodeLifecycleProtocol
from app.lifecycle.services.operator_node import OperatorNodeLifecycleService


def test_operator_node_lifecycle_service_satisfies_protocols() -> None:
    svc = OperatorNodeLifecycleService.__new__(OperatorNodeLifecycleService)
    assert isinstance(svc, OperatorNodeLifecycleProtocol)
    assert isinstance(svc, OperatorNodeManager)
