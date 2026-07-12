import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


def _make_service(lifecycle: object | None = None) -> SessionSyncService:
    return SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle=lifecycle if lifecycle is not None else AsyncMock(),
    )


async def test_sync_commits_without_deferred_stop_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()

    svc = _make_service()
    monkeypatch.setattr(svc, "_check_liveness", AsyncMock())
    monkeypatch.setattr(svc, "_kill_orphans", AsyncMock())
    await svc.sync(db)

    db.commit.assert_awaited_once()


async def test_restore_skips_when_session_still_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A device with a still-running session is left alone (no lifecycle call)."""
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: SimpleNamespace(id=1)))
    )
    mock_lifecycle = AsyncMock()

    svc = _make_service(lifecycle=mock_lifecycle)
    await svc._restore_device_after_session_end(db, uuid.uuid4())

    mock_lifecycle.handle_session_finished.assert_not_awaited()


def _no_running_then_device(device: object | None) -> AsyncMock:
    """db.execute that returns 'no running session' (scalars().first() -> None) then the
    device row (scalar_one_or_none() -> device)."""
    return AsyncMock(
        side_effect=[
            SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None)),
            SimpleNamespace(scalar_one_or_none=lambda: device),
        ]
    )


async def test_restore_returns_when_device_missing() -> None:
    """No running session, but the device row vanished -> return before lifecycle."""
    db = MagicMock()
    db.execute = _no_running_then_device(None)
    mock_lifecycle = AsyncMock()

    svc = _make_service(lifecycle=mock_lifecycle)
    await svc._restore_device_after_session_end(db, uuid.uuid4())

    mock_lifecycle.handle_session_finished.assert_not_awaited()


@pytest.mark.parametrize("outcome_name", ["AUTO_STOPPED", "RUNNING_SESSION_EXISTS"])
async def test_restore_returns_on_terminal_lifecycle_outcome(outcome_name: str) -> None:
    """AUTO_STOPPED and RUNNING_SESSION_EXISTS short-circuit before the locked recheck."""
    from app.lifecycle.services import policy as lifecycle_policy

    device = SimpleNamespace(id=uuid.uuid4())
    db = MagicMock()
    db.execute = _no_running_then_device(device)
    db.get = AsyncMock()
    mock_lifecycle = AsyncMock()
    mock_lifecycle.handle_session_finished = AsyncMock(
        return_value=getattr(lifecycle_policy.DeferredStopOutcome, outcome_name)
    )

    svc = _make_service(lifecycle=mock_lifecycle)
    await svc._restore_device_after_session_end(db, device.id)

    mock_lifecycle.handle_session_finished.assert_awaited_once()
    # No lock taken (no locked recheck path) on the terminal outcomes.
    db.get.assert_not_called()
