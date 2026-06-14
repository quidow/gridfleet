from app.runs.protocols import RunAllocatorProtocol
from app.runs.service_allocator import RunAllocatorService


def test_run_allocator_service_satisfies_protocol() -> None:
    assert isinstance(RunAllocatorService.__new__(RunAllocatorService), RunAllocatorProtocol)
