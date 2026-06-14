from app.runs.protocols import RunLifecycleProtocol
from app.runs.service_lifecycle import RunLifecycleService


def test_run_lifecycle_service_satisfies_protocol() -> None:
    assert isinstance(RunLifecycleService.__new__(RunLifecycleService), RunLifecycleProtocol)
