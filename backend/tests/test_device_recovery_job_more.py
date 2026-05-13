from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import device_recovery_job
from app.services.job_status_constants import JOB_STATUS_FAILED


class RecoverySession:
    def __init__(self, row: SimpleNamespace | None) -> None:
        self.row = row
        self.committed = False

    async def __aenter__(self) -> RecoverySession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, *_args: object, **_kwargs: object) -> SimpleNamespace | None:
        return self.row

    async def commit(self) -> None:
        self.committed = True


class RecoverySessionFactory:
    def __init__(self, *sessions: RecoverySession) -> None:
        self.sessions = list(sessions)

    def __call__(self) -> RecoverySession:
        return self.sessions.pop(0)


def _job_row() -> SimpleNamespace:
    return SimpleNamespace(
        status="running",
        snapshot={"status": "running"},
        completed_at=None,
    )


async def test_device_recovery_job_marks_failed_when_lock_fails() -> None:
    job_id = str(uuid.uuid4())
    device_id = uuid.uuid4()
    row = _job_row()
    session = RecoverySession(row)

    with patch("app.services.device_recovery_job.device_locking.lock_device", new=AsyncMock(side_effect=RuntimeError)):
        await device_recovery_job.run_device_recovery_job(
            job_id,
            {"device_id": str(device_id)},
            session_factory=RecoverySessionFactory(session),  # type: ignore[arg-type]
        )

    assert row.status == JOB_STATUS_FAILED
    assert row.snapshot["status"] == JOB_STATUS_FAILED
    assert f"Device {device_id}" in row.snapshot["error"]
    assert row.completed_at is not None
    assert session.committed is True


async def test_device_recovery_job_marks_failed_when_recovery_crashes() -> None:
    job_id = str(uuid.uuid4())
    device_id = uuid.uuid4()
    first_row = _job_row()
    failure_row = _job_row()
    first_session = RecoverySession(first_row)
    failure_session = RecoverySession(failure_row)

    with (
        patch("app.services.device_recovery_job.device_locking.lock_device", new=AsyncMock(return_value=object())),
        patch(
            "app.services.device_recovery_job.lifecycle_policy.attempt_auto_recovery",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await device_recovery_job.run_device_recovery_job(
            job_id,
            {"device_id": str(device_id), "source": "manual", "reason": "operator"},
            session_factory=RecoverySessionFactory(first_session, failure_session),  # type: ignore[arg-type]
        )

    assert failure_row.status == JOB_STATUS_FAILED
    assert failure_row.snapshot["error"] == "device_recovery job crashed unexpectedly"
    assert failure_row.completed_at is not None
    assert failure_session.committed is True
