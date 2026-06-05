import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.sessions import service_sync as session_sync
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus


def _make_service(lifecycle: object | None = None) -> SessionSyncService:
    return SessionSyncService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        lifecycle=lifecycle if lifecycle is not None else AsyncMock(),
    )


async def test_sweep_stale_stop_pending_handles_deleted_rows() -> None:
    db = MagicMock()
    missing_id = uuid.uuid4()
    device = SimpleNamespace(id=uuid.uuid4())
    db.execute = AsyncMock(
        return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [missing_id, device.id]))
    )
    db.get = AsyncMock(side_effect=[None, device])
    complete = AsyncMock()
    mock_lifecycle = AsyncMock()
    mock_lifecycle.complete_deferred_stop_if_session_ended = complete

    svc = _make_service(lifecycle=mock_lifecycle)
    await svc._sweep_stale_stop_pending(db)

    complete.assert_awaited_once_with(db, device)


async def test_sync_commits_after_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())

    svc = _make_service()
    monkeypatch.setattr(svc, "_check_liveness", AsyncMock())
    monkeypatch.setattr(svc, "_kill_orphans", AsyncMock())
    sweep = AsyncMock()
    monkeypatch.setattr(svc, "_sweep_stale_stop_pending", sweep)

    await svc.sync(db)

    sweep.assert_awaited_once_with(db)
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
