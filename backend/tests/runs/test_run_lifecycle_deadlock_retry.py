"""Terminal run transitions (complete / cancel / force-release) retry on a
Postgres deadlock instead of surfacing a 500.

A terminal transition races teardown traffic on the same device and session
rows (terminal session-status writes, background reconciles). Postgres resolves
such a collision by killing one transaction with sqlstate 40P01. Before the
retry, that error bubbled to the client as a 500 and the run kept its device
reservations until the reaper expired it — starving the next run's allocation
for the whole heartbeat-timeout window.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import DBAPIError

from app.runs.models import RunState
from app.runs.service_lifecycle import RunLifecycleService
from tests.helpers import test_event_bus as event_bus


class _FakeDeadlockError(Exception):
    sqlstate = "40P01"


class _FakeOtherDbError(Exception):
    sqlstate = "23505"


def _deadlock_error() -> DBAPIError:
    return DBAPIError("UPDATE sessions SET ended_at=...", None, _FakeDeadlockError())


def _other_db_error() -> DBAPIError:
    return DBAPIError("INSERT INTO ...", None, _FakeOtherDbError())


def _run(state: RunState = RunState.active) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="mock-run",
        state=state,
        started_at=None,
        completed_at=None,
        last_heartbeat=None,
        error=None,
        device_reservations=[],
    )


def _mock_release(release_devices: AsyncMock | None = None) -> AsyncMock:
    mock = AsyncMock()
    mock.release_devices = release_devices if release_devices is not None else AsyncMock(return_value=[])
    mock.clear_desired_grid_run_id_for_run = AsyncMock()
    mock.complete_deferred_stops_post_commit = AsyncMock()
    return mock


def _make_lifecycle(release: AsyncMock) -> RunLifecycleService:
    from app.settings.service import SettingsService

    return RunLifecycleService(publisher=event_bus, settings=SettingsService(), release=release)


def _fresh_run_getter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each (re-)attempt re-reads the run; after a rollback the previous
    attempt's in-memory state mutation is gone, so hand out a fresh
    non-terminal run per call."""

    async def fresh_run(db: object, run_id: object) -> SimpleNamespace:
        del db, run_id
        return _run(RunState.active)

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", fresh_run)
    monkeypatch.setattr("app.runs.service_lifecycle.get_run", AsyncMock(return_value=_run(RunState.cancelled)))
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    # The retry now lives in app.core.db_retry; its backoff is a def-time-bound
    # default parameter, so neutralize the inter-attempt sleep at its source.
    monkeypatch.setattr("app.core.db_retry.asyncio.sleep", AsyncMock())


@pytest.mark.parametrize("fn_name", ["complete_run", "cancel_run", "force_release"])
async def test_terminal_transition_retries_past_transient_deadlock(
    monkeypatch: pytest.MonkeyPatch, fn_name: str
) -> None:
    _fresh_run_getter(monkeypatch)
    release_devices = AsyncMock(side_effect=[_deadlock_error(), []])
    release = _mock_release(release_devices)
    lifecycle = _make_lifecycle(release)
    db = AsyncMock()

    result = await getattr(lifecycle, fn_name)(db, uuid.uuid4())

    assert result is not None
    assert release_devices.await_count == 2, "transition must be retried after a deadlock"
    db.rollback.assert_awaited()


async def test_terminal_transition_gives_up_after_bounded_deadlock_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.db_retry import _DEFAULT_ATTEMPTS

    _fresh_run_getter(monkeypatch)
    release_devices = AsyncMock(side_effect=[_deadlock_error() for _ in range(_DEFAULT_ATTEMPTS)])
    release = _mock_release(release_devices)
    lifecycle = _make_lifecycle(release)
    db = AsyncMock()

    with pytest.raises(DBAPIError):
        await lifecycle.cancel_run(db, uuid.uuid4())

    assert release_devices.await_count == _DEFAULT_ATTEMPTS


async def test_terminal_transition_does_not_retry_non_deadlock_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fresh_run_getter(monkeypatch)
    release_devices = AsyncMock(side_effect=_other_db_error())
    release = _mock_release(release_devices)
    lifecycle = _make_lifecycle(release)
    db = AsyncMock()

    with pytest.raises(DBAPIError):
        await lifecycle.cancel_run(db, uuid.uuid4())

    assert release_devices.await_count == 1, "only deadlocks are retried"
