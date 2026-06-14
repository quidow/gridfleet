from app.appium_nodes.protocols import ReconcilerProtocol
from app.appium_nodes.services.reconciler import ReconcilerService
from app.devices.protocols import NodeConvergence


def test_reconciler_service_satisfies_protocols() -> None:
    svc = ReconcilerService.__new__(ReconcilerService)
    assert isinstance(svc, ReconcilerProtocol)
    assert isinstance(svc, NodeConvergence)
