from app.lifecycle.protocols import LifecycleIncidentRecorder
from app.lifecycle.services.incidents import LifecycleIncidentService


def test_lifecycle_incident_service_satisfies_protocols() -> None:
    svc = LifecycleIncidentService()
    assert isinstance(svc, LifecycleIncidentRecorder)
