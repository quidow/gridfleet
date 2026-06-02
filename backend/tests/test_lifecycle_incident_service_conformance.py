from app.appium_nodes.protocols import LifecycleIncidentRecorder as AppiumRecorder
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.protocols import LifecycleIncidentRecorder as RunsRecorder


def test_lifecycle_incident_service_satisfies_protocols() -> None:
    svc = LifecycleIncidentService()
    assert isinstance(svc, RunsRecorder)
    assert isinstance(svc, AppiumRecorder)
