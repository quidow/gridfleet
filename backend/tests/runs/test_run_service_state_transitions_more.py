from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runs.models import RunState
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_teardown import RunTeardownKind
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


def _delegating_lifecycle(teardown: AsyncMock) -> RunLifecycleService:
    from app.settings.service import SettingsService

    return RunLifecycleService(
        publisher=event_bus,
        settings=SettingsService(),
        release=_mock_release(),
        session_factory=_FakeSessionFactory(),
        teardown=teardown,
    )


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

    # complete_run stays a lifecycle-owned single transaction.
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.complete_run(uuid.uuid4())
    monkeypatch.setattr(
        "app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=_run(RunState.completed))
    )
    with pytest.raises(ValueError, match="terminal state"):
        await lifecycle.complete_run(uuid.uuid4())

    # cancel / force-release now run the durable teardown; the reject guards live
    # in RunTeardownService.prepare, before any Appium/job work touches the DB.
    monkeypatch.setattr("app.runs.service_teardown.get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.cancel_run(uuid.uuid4())
    with pytest.raises(ValueError, match="Run not found"):
        await lifecycle.force_release(uuid.uuid4())
    monkeypatch.setattr(
        "app.runs.service_teardown.get_run_for_update", AsyncMock(return_value=_run(RunState.completed))
    )
    with pytest.raises(ValueError, match="terminal state"):
        await lifecycle.cancel_run(uuid.uuid4())

    # A successful terminal teardown is delegated to the durable collaborator.
    teardown = AsyncMock()
    delegating = _delegating_lifecycle(teardown)
    cancellable = _run(RunState.active)
    assert (await delegating.cancel_run(cancellable.id)).run_id == cancellable.id
    teardown.teardown_run.assert_awaited_with(RunTeardownKind.cancel, cancellable.id)
    releasable = _run(RunState.active)
    assert (await delegating.force_release(releasable.id)).run_id == releasable.id
    teardown.teardown_run.assert_awaited_with(RunTeardownKind.force_release, releasable.id)


async def test_expire_run_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_release = _mock_release()
    lifecycle = _make_lifecycle(mock_release)
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)

    # Missing / terminal runs are silent no-ops in the durable prepare step.
    monkeypatch.setattr("app.runs.service_teardown.get_run_for_update", AsyncMock(return_value=None))
    await lifecycle.expire_run(uuid.uuid4(), "timeout")

    terminal = _run(RunState.completed)
    monkeypatch.setattr("app.runs.service_teardown.get_run_for_update", AsyncMock(return_value=terminal))
    await lifecycle.expire_run(terminal.id, "timeout")
    assert terminal.state == RunState.completed

    # An active run's expiry is delegated to the durable teardown collaborator.
    teardown = AsyncMock()
    delegating = _delegating_lifecycle(teardown)
    run_id = uuid.uuid4()
    await delegating.expire_run(run_id, "timeout")
    teardown.teardown_run.assert_awaited_with(RunTeardownKind.expire, run_id, "timeout")


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
