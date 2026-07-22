from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.jobs.statuses import JOB_STATUS_FAILED
from app.lifecycle.services import recovery_job as device_recovery_job
from tests.fakes import FakeSettingsReader


@pytest.fixture(autouse=True)
def _speed_up_recovery_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_recovery_job, "RECOVERY_NODE_START_WAIT_POLL_SEC", 0)
    monkeypatch.setattr(device_recovery_job, "RECOVERY_PROBE_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(device_recovery_job, "RECOVERY_PROBE_JITTER_MAX_SEC", 0)


class RecoverySession:
    def __init__(self, row: SimpleNamespace | None, *, query_row: SimpleNamespace | None = None) -> None:
        self.row = row
        self._query_row = query_row
        self.committed = False
        self._in_txn = False
        self._query_call = 0

    async def __aenter__(self) -> RecoverySession:
        self._in_txn = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self._in_txn = False

    async def get(self, *_args: object, **_kwargs: object) -> SimpleNamespace | None:
        return self.row

    async def commit(self) -> None:
        self.committed = True
        self._in_txn = False

    async def rollback(self) -> None:
        self._in_txn = False

    async def execute(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        self._query_call += 1
        row = self._query_row
        if self._query_call == 1 and row is not None:
            return SimpleNamespace(one_or_none=lambda: SimpleNamespace(pid=None, active_connection_target=None))
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            one_or_none=lambda: row,
            scalars=lambda: SimpleNamespace(all=lambda: []),
        )


class RecoverySessionFactory:
    def __init__(self, *sessions: RecoverySession, repeat: RecoverySession | None = None) -> None:
        self.sessions = list(sessions)
        self._repeat = repeat

    def __call__(self) -> RecoverySession:
        if self._repeat is not None and not self.sessions:
            return self._repeat
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
    prepare_session = RecoverySession(row)

    with patch(
        "app.lifecycle.services.recovery_job.device_locking.lock_device_handle",
        new=AsyncMock(side_effect=Exception("lock failed")),
    ):
        await device_recovery_job.RecoveryJobService(
            session_factory=RecoverySessionFactory(prepare_session),  # type: ignore[arg-type]
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            lifecycle_policy=AsyncMock(),
            viability=AsyncMock(),
        ).run_device_recovery_job(
            job_id,
            {"device_id": str(device_id)},
        )

    assert row.status == JOB_STATUS_FAILED
    assert row.snapshot["status"] == JOB_STATUS_FAILED
    assert f"Device {device_id}" in row.snapshot["error"]
    assert row.completed_at is not None
    assert prepare_session.committed is True


async def test_device_recovery_job_marks_failed_when_recovery_crashes() -> None:
    job_id = str(uuid.uuid4())
    device_id = uuid.uuid4()
    first_row = _job_row()
    failure_row = _job_row()
    prepare_session = RecoverySession(first_row)
    clear_session = RecoverySession(None)
    failure_session = RecoverySession(failure_row)

    mock_lifecycle_policy = AsyncMock()
    mock_lifecycle_policy.prepare_auto_recovery_locked = AsyncMock(side_effect=RuntimeError("boom"))

    locked = Mock(
        device=SimpleNamespace(
            id=device_id,
            appium_node=None,
            lifecycle_policy_state={},
        )
    )
    with patch(
        "app.lifecycle.services.recovery_job.device_locking.lock_device_handle",
        new=AsyncMock(return_value=locked),
    ):
        await device_recovery_job.RecoveryJobService(
            session_factory=RecoverySessionFactory(prepare_session, clear_session, failure_session),  # type: ignore[arg-type]
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            lifecycle_policy=mock_lifecycle_policy,
            viability=AsyncMock(),
        ).run_device_recovery_job(
            job_id,
            {"device_id": str(device_id), "source": "manual", "reason": "operator"},
        )

    assert failure_row.status == JOB_STATUS_FAILED
    assert failure_row.snapshot["error"] == "device_recovery job crashed unexpectedly"
    assert failure_row.completed_at is not None
    assert failure_session.committed is True


async def test_malformed_payload_marks_job_failed_instead_of_raising() -> None:
    """A payload without device_id must not escape as KeyError; the job row goes FAILED."""
    row = _job_row()
    failure_session = RecoverySession(row)

    await device_recovery_job.RecoveryJobService(
        session_factory=RecoverySessionFactory(failure_session),  # type: ignore[arg-type]
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        lifecycle_policy=AsyncMock(),
        viability=AsyncMock(),
    ).run_device_recovery_job(str(uuid.uuid4()), {})

    assert row.status == JOB_STATUS_FAILED
    assert row.snapshot["status"] == JOB_STATUS_FAILED
    assert row.snapshot["error"] == "device_recovery job crashed unexpectedly"
    assert failure_session.committed is True


async def test_polling_sleeps_observe_no_open_transaction() -> None:
    """Every ``asyncio.sleep`` call during node-start polling must observe the
    worker's session closed (no open transaction)."""
    import asyncio

    job_id = str(uuid.uuid4())
    device_id = uuid.uuid4()
    prepare_row = _job_row()
    finalize_row = _job_row()
    prepare_session = RecoverySession(prepare_row)
    poll_query_row = SimpleNamespace(pid=12345, active_connection_target="127.0.0.1:4723")
    poll_session = RecoverySession(None, query_row=poll_query_row)
    finalize_session = RecoverySession(finalize_row)
    job_session = RecoverySession(prepare_row)

    sleep_calls: list[bool] = []
    real_sleep = asyncio.sleep

    async def _capturing_sleep(seconds: float) -> None:
        sleep_calls.append(poll_session._in_txn)
        await real_sleep(0)

    node = SimpleNamespace(id=uuid.uuid4(), pid=None, active_connection_target=None)
    locked = Mock(
        device=SimpleNamespace(
            id=device_id,
            appium_node=node,
            lifecycle_policy_state={},
        )
    )

    mock_lifecycle_policy = AsyncMock()
    mock_lifecycle_policy.prepare_auto_recovery_locked = AsyncMock(return_value=True)
    mock_lifecycle_policy.finalize_auto_recovery_locked = AsyncMock(return_value="recovered")

    with (
        patch(
            "app.lifecycle.services.recovery_job.device_locking.lock_device_handle",
            new=AsyncMock(return_value=locked),
        ),
        patch("app.lifecycle.services.recovery_job.asyncio.sleep", new=_capturing_sleep),
        patch(
            "app.lifecycle.services.recovery_job.load_device_decision_snapshot",
            new=AsyncMock(return_value=Mock(recovery_generation=None)),
        ),
        patch.object(
            device_recovery_job.RecoveryJobService,
            "_run_probe",
            new=AsyncMock(return_value={"status": "passed"}),
        ),
        patch.object(device_recovery_job, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 1.0),
    ):
        await device_recovery_job.RecoveryJobService(
            session_factory=RecoverySessionFactory(
                prepare_session, poll_session, finalize_session, job_session, repeat=poll_session
            ),  # type: ignore[arg-type]
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            lifecycle_policy=mock_lifecycle_policy,
            viability=AsyncMock(),
        ).run_device_recovery_job(
            job_id,
            {"device_id": str(device_id), "source": "manual", "reason": "operator"},
        )

    assert sleep_calls, "polling sleep was never called"
    assert all(not in_txn for in_txn in sleep_calls), "a sleep ran with an open transaction"
    _ = prepare_row, finalize_row  # suppress unused warnings
