from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.runs import service as run_service
from app.runs.models import RunState
from tests.helpers import test_event_bus as event_bus


def _run(state: RunState = RunState.preparing) -> SimpleNamespace:
    return SimpleNamespace(
        id=__import__("uuid").uuid4(),
        name="mock-run",
        state=state,
        started_at=None,
        completed_at=None,
        last_heartbeat=None,
        error=None,
        device_reservations=[],
    )


def _db() -> AsyncMock:
    db = AsyncMock()
    db.commit = AsyncMock()
    return db


async def test_signal_active_and_device_session_state_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db()
    run = _run(RunState.preparing)
    monkeypatch.setattr("app.runs.service_lifecycle.queue_event_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=run))
    monkeypatch.setattr("app.runs.service_lifecycle.get_run", AsyncMock(return_value=run))

    active = await run_service.signal_active(db, run.id, publisher=event_bus)
    assert active.state == RunState.active
    assert active.started_at is not None
    assert active.last_heartbeat is not None

    already_active = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=already_active))
    assert await run_service.signal_active(db, already_active.id, publisher=event_bus) is already_active

    with pytest.raises(ValueError, match="Run not found"):
        monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
        await run_service.signal_active(db, __import__("uuid").uuid4(), publisher=event_bus)

    terminal = _run(RunState.completed)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=terminal))
    with pytest.raises(ValueError, match="Cannot signal active"):
        await run_service.signal_active(db, terminal.id, publisher=event_bus)

    # Regression: there is no observed-session helper. The only public path
    # out of preparing is signal_ready / signal_active, both driven by an
    # explicit client signal.
    assert not hasattr(run_service, "signal_active_for_device_session")
    assert not hasattr(run_service, "signal_active_for_device_session_no_commit")


async def test_run_terminal_transitions_success_and_guard_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _db()
    monkeypatch.setattr("app.runs.service_lifecycle.queue_event_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.runs.service_lifecycle._clear_desired_grid_run_id_for_run", AsyncMock())
    monkeypatch.setattr("app.runs.service_lifecycle._release_devices", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.runs.service_lifecycle._complete_deferred_stops_post_commit", AsyncMock())

    active = _run(RunState.active)
    active.started_at = datetime.now(UTC)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=active))
    monkeypatch.setattr("app.runs.service_lifecycle.get_run", AsyncMock(return_value=active))
    completed = await run_service.complete_run(db, active.id, publisher=event_bus)
    assert completed.state == RunState.completed
    assert completed.completed_at is not None

    for fn in (run_service.complete_run, run_service.cancel_run):
        monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
        with pytest.raises(ValueError, match="Run not found"):
            await fn(db, __import__("uuid").uuid4(), publisher=event_bus)
        monkeypatch.setattr(
            "app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=_run(RunState.completed))
        )
        with pytest.raises(ValueError, match="terminal state"):
            await fn(db, __import__("uuid").uuid4(), publisher=event_bus)

    cancellable = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=cancellable))
    monkeypatch.setattr("app.runs.service_lifecycle.get_run", AsyncMock(return_value=cancellable))
    cancelled = await run_service.cancel_run(db, cancellable.id, publisher=event_bus)
    assert cancelled.state == RunState.cancelled

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.force_release(db, __import__("uuid").uuid4(), publisher=event_bus)

    releasable = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=releasable))
    monkeypatch.setattr("app.runs.service_lifecycle.get_run", AsyncMock(return_value=releasable))
    forced = await run_service.force_release(db, releasable.id, publisher=event_bus)
    assert forced.state == RunState.cancelled
    assert forced.error == "Force released by admin"

    missing = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    await run_service.expire_run(db, missing, "timeout", publisher=event_bus)

    terminal = _run(RunState.completed)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=terminal))
    await run_service.expire_run(db, terminal, "timeout", publisher=event_bus)
    assert terminal.state == RunState.completed

    expiring = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=expiring))
    await run_service.expire_run(db, expiring, "timeout", publisher=event_bus)
    assert expiring.state == RunState.expired
    assert expiring.error == "timeout"

    heartbeat_run = _run(RunState.active)
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=heartbeat_run))
    monkeypatch.setattr("app.runs.service_lifecycle.get_run", AsyncMock(return_value=heartbeat_run))
    assert await run_service.heartbeat(db, heartbeat_run.id) is heartbeat_run
    assert heartbeat_run.last_heartbeat is not None

    terminal_heartbeat = _run(RunState.completed)
    before = terminal_heartbeat.last_heartbeat
    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=terminal_heartbeat))
    assert await run_service.heartbeat(db, terminal_heartbeat.id) is terminal_heartbeat
    assert terminal_heartbeat.last_heartbeat == before

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="Run not found"):
        await run_service.heartbeat(db, __import__("uuid").uuid4())
