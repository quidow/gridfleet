from app.appium_nodes.protocols import ReconcilerProtocol
from app.appium_nodes.services.reconciler import ReconcilerService


def test_reconciler_service_satisfies_protocol() -> None:
    assert isinstance(ReconcilerService.__new__(ReconcilerService), ReconcilerProtocol)
