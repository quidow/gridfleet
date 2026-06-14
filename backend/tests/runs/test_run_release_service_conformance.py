from app.runs.protocols import RunReleaseProtocol
from app.runs.service_lifecycle_release import RunReleaseService


def test_run_release_service_satisfies_protocol() -> None:
    assert isinstance(RunReleaseService.__new__(RunReleaseService), RunReleaseProtocol)
