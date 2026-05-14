from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.devices.services import verification_job_state as job_state


async def test_device_verification_job_state_persist_and_stage_resolution_branches() -> None:
    class Session:
        def __init__(self, row: object | None) -> None:
            self.row = row
            self.commit = AsyncMock()

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> object | None:
            return self.row

    missing_job = job_state.hydrate_job(
        job_state.new_job("missing"), db_job_id="missing", session_factory=lambda: Session(None)
    )
    await job_state.persist_job(missing_job)

    row = SimpleNamespace(snapshot=None, status=None, completed_at=None)
    completed = job_state.hydrate_job(job_state.new_job("done"), db_job_id="done", session_factory=lambda: Session(row))
    completed["status"] = "completed"
    completed["finished_at"] = "2026-05-13T12:00:00+00:00"
    await job_state.persist_job(completed)
    assert row.status == "completed"
    assert row.completed_at is not None

    failed = job_state.new_job("failed")
    failed["status"] = "failed"
    failed["stages"][0]["status"] = "failed"
    assert job_state.public_snapshot(failed)["current_stage"] == "validation"

    fallback = job_state.new_job("fallback")
    fallback["stages"][0]["status"] = "passed"
    assert job_state.public_snapshot(fallback)["current_stage"] == "validation"
