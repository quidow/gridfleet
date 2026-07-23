from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runs.models import RunState
from app.runs.service_lifecycle import RunLifecycleService
from tests.helpers import test_event_bus as event_bus


def _run(state: RunState = RunState.preparing) -> SimpleNamespace:
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


class _FakeBegin:
    def __init__(self, db: object) -> None:
        self._db = db

    async def __aenter__(self) -> object:
        return self._db

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeSessionFactory:
    """Hands out one AsyncMock session per begin()/call — good enough for the
    lifecycle command's transaction-local + post-commit deferred-stop passes."""

    def __init__(self) -> None:
        self.db = AsyncMock()

    def begin(self) -> _FakeBegin:
        return _FakeBegin(self.db)

    def __call__(self) -> _FakeBegin:
        return _FakeBegin(self.db)


def _mock_release() -> AsyncMock:
    mock = AsyncMock()
    mock.lock_run_devices = AsyncMock(return_value={})
    mock.release_devices = AsyncMock(return_value=[])
    mock.clear_desired_grid_run_id_for_run = AsyncMock()
    mock.terminate_run_sessions_and_probe_survivors = AsyncMock(return_value=set())
    mock.complete_deferred_stops_post_commit = AsyncMock()
    return mock


def _make_lifecycle(release: AsyncMock | None = None) -> RunLifecycleService:
    from app.settings.service import SettingsService

    return RunLifecycleService(
        publisher=event_bus,
        settings=SettingsService(),
        release=release if release is not None else _mock_release(),
        session_factory=_FakeSessionFactory(),
    )


async def test_signal_active_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    lifecycle = _make_lifecycle()

    preparing = _run(RunState.preparing)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=preparing))
    result = await lifecycle.signal_active(preparing.id)
    assert result.run_id == preparing.id
    assert preparing.state == RunState.active
    assert preparing.started_at is not None
    assert preparing.last_heartbeat is not None

    already_active = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=already_active))
    assert (await lifecycle.signal_active(already_active.id)).run_id == already_active.id

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.signal_active(uuid.uuid4())

    terminal = _run(RunState.completed)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=terminal))
    with pytest.raises(ValueError, match="Cannot signal active"):
        await lifecycle.signal_active(terminal.id)


async def test_signal_ready_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    lifecycle = _make_lifecycle()

    preparing = _run(RunState.preparing)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=preparing))
    result = await lifecycle.signal_ready(preparing.id)
    assert result.run_id == preparing.id
    assert preparing.state == RunState.active

    active = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=active))
    with pytest.raises(ValueError, match="Cannot signal ready"):
        await lifecycle.signal_ready(active.id)


async def test_terminal_transitions_success_and_guards(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_release = _mock_release()
    lifecycle = _make_lifecycle(mock_release)

    active = _run(RunState.active)
    active.started_at = datetime.now(UTC)
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=active))
    result = await lifecycle.complete_run(active.id)
    assert result.run_id == active.id
    assert active.state == RunState.completed
    assert active.completed_at is not None

    for fn_name in ("complete_run", "cancel_run"):
        fn = getattr(lifecycle, fn_name)
        monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="Run not found"):
            await fn(uuid.uuid4())
        monkeypatch.setattr(
            "app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=_run(RunState.completed))
        )
        with pytest.raises(ValueError, match="terminal state"):
            await fn(uuid.uuid4())

    cancellable = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=cancellable))
    assert (await lifecycle.cancel_run(cancellable.id)).run_id == cancellable.id
    assert cancellable.state == RunState.cancelled

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.force_release(uuid.uuid4())

    releasable = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=releasable))
    forced = await lifecycle.force_release(releasable.id)
    assert forced.run_id == releasable.id
    assert releasable.state == RunState.cancelled
    assert releasable.error == "Force released by admin"


async def test_expire_run_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_release = _mock_release()
    lifecycle = _make_lifecycle(mock_release)
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    db = AsyncMock()

    missing = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    await lifecycle.expire_run(db, missing, "timeout")

    terminal = _run(RunState.completed)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=terminal))
    await lifecycle.expire_run(db, terminal, "timeout")
    assert terminal.state == RunState.completed

    expiring = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=expiring))
    await lifecycle.expire_run(db, expiring, "timeout")
    assert expiring.state == RunState.expired
    assert expiring.error == "timeout"


async def test_heartbeat_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    lifecycle = _make_lifecycle()

    heartbeat_run = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=heartbeat_run))
    assert (await lifecycle.heartbeat(heartbeat_run.id)).run_id == heartbeat_run.id
    assert heartbeat_run.last_heartbeat is not None

    terminal_heartbeat = _run(RunState.completed)
    before = terminal_heartbeat.last_heartbeat
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=terminal_heartbeat))
    assert (await lifecycle.heartbeat(terminal_heartbeat.id)).run_id == terminal_heartbeat.id
    assert terminal_heartbeat.last_heartbeat == before

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.heartbeat(uuid.uuid4())


async def test_no_observed_session_helper() -> None:
    from app.runs import service as run_service

    assert not hasattr(run_service, "signal_active_for_device_session")
    assert not hasattr(run_service, "signal_active_for_device_session_no_commit")
