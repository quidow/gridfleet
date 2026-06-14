from app.runs.protocols import RunQueryProtocol
from app.runs.service_query import RunQueryService


def test_run_query_service_satisfies_protocol() -> None:
    assert isinstance(RunQueryService.__new__(RunQueryService), RunQueryProtocol)
