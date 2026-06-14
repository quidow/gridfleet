from app.runs.protocols import RunFailureProtocol
from app.runs.service_lifecycle_failures import RunFailureService


def test_run_failure_service_satisfies_protocol() -> None:
    assert isinstance(RunFailureService.__new__(RunFailureService), RunFailureProtocol)
