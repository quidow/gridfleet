from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
from app.devices.protocols import RemoteNodeManager


def test_reconciler_agent_service_satisfies_protocols() -> None:
    svc = ReconcilerAgentService.__new__(ReconcilerAgentService)
    assert isinstance(svc, RemoteNodeManager)
