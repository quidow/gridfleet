"""Terminal run transitions (complete / cancel / force-release) retry on a
Postgres deadlock instead of surfacing a 500.

The retry now opens a FRESH session per attempt (``session_factory.begin()``),
re-reads and re-locks the run, its reserved devices in sorted order, then the
reservation children — the root -> sorted-device -> child order the
deadlock-avoidance contract requires.
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


class _FakeDb:
    def __init__(self, session_id: uuid.UUID) -> None:
        self.id = session_id
        self.lock_order: list[object] = []


class _FakeBegin:
    def __init__(self, db: _FakeDb) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeDb:
        return self._db

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeSessionFactory:
    def __init__(self) -> None:
        self.dbs: list[_FakeDb] = []

    def begin(self) -> _FakeBegin:
        db = _FakeDb(uuid.uuid4())
        self.dbs.append(db)
        return _FakeBegin(db)

    def __call__(self) -> _FakeBegin:
        db = _FakeDb(uuid.uuid4())
        self.dbs.append(db)
        return _FakeBegin(db)


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="mock-run",
        state=RunState.active,
        started_at=None,
        completed_at=None,
        last_heartbeat=None,
        error=None,
        device_reservations=[],
    )


def _make_lifecycle(release: AsyncMock, factory: _FakeSessionFactory) -> RunLifecycleService:
    from app.settings.service import SettingsService

    return RunLifecycleService(
        publisher=event_bus, settings=SettingsService(), release=release, session_factory=factory
    )


@pytest.mark.parametrize("fn_name", ["complete_run", "cancel_run", "force_release"])
async def test_terminal_transition_retries_with_fresh_session_and_ordered_locks(
    monkeypatch: pytest.MonkeyPatch, fn_name: str
) -> None:
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.core.db_retry.asyncio.sleep", AsyncMock())

    sorted_device_ids = sorted([uuid.uuid4(), uuid.uuid4()])

    factory = _FakeSessionFactory()
    locked_run_reads = 0

    async def fake_get_run(db: _FakeDb, run_id: object) -> SimpleNamespace:
        nonlocal locked_run_reads
        locked_run_reads += 1
        db.lock_order.append("run")
        return _run()

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", fake_get_run)

    async def lock_run_devices(db: _FakeDb, run: object) -> dict[uuid.UUID, object]:
        for device_id in sorted_device_ids:
            db.lock_order.append(device_id)
        return {device_id: SimpleNamespace(device=SimpleNamespace(id=device_id)) for device_id in sorted_device_ids}

    call_count = 0

    async def release_devices(db: _FakeDb, run: object, *, locked_by_id: object) -> list[uuid.UUID]:
        nonlocal call_count
        db.lock_order.append("reservation")
        call_count += 1
        if call_count == 1:
            raise _deadlock_error()
        return []

    release = AsyncMock()
    release.lock_run_devices = AsyncMock(side_effect=lock_run_devices)
    release.release_devices = AsyncMock(side_effect=release_devices)
    release.clear_desired_grid_run_id_for_run = AsyncMock()
    release.terminate_run_sessions_and_probe_survivors = AsyncMock(return_value=set())
    release.complete_deferred_stops_post_commit = AsyncMock()

    lifecycle = _make_lifecycle(release, factory)

    result = await getattr(lifecycle, fn_name)(uuid.uuid4())

    assert result is not None
    assert release.release_devices.await_count == 2, "transition must be retried after a deadlock"

    attempt_session_ids = [db.id for db in factory.dbs]
    assert attempt_session_ids[0] != attempt_session_ids[1], "each attempt must open a fresh session"
    assert locked_run_reads == 2, "each attempt re-reads and re-locks the run"

    lock_orders = [db.lock_order for db in factory.dbs]
    assert all(order == ["run", *sorted_device_ids, "reservation"] for order in lock_orders), lock_orders


async def test_terminal_transition_gives_up_after_bounded_deadlock_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.db_retry import _DEFAULT_ATTEMPTS

    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.core.db_retry.asyncio.sleep", AsyncMock())

    async def fresh_run(db: object, run_id: object) -> SimpleNamespace:
        del db, run_id
        return _run()

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", fresh_run)

    release = AsyncMock()
    release.lock_run_devices = AsyncMock(return_value={})
    release.clear_desired_grid_run_id_for_run = AsyncMock()
    release.release_devices = AsyncMock(side_effect=[_deadlock_error() for _ in range(_DEFAULT_ATTEMPTS)])
    release.complete_deferred_stops_post_commit = AsyncMock()

    lifecycle = _make_lifecycle(release, _FakeSessionFactory())

    with pytest.raises(DBAPIError):
        await lifecycle.cancel_run(uuid.uuid4())

    assert release.release_devices.await_count == _DEFAULT_ATTEMPTS


async def test_terminal_transition_does_not_retry_non_deadlock_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.events.event_bus.EventBus.queue_for_session", lambda *args, **kwargs: None)
    monkeypatch.setattr("app.core.db_retry.asyncio.sleep", AsyncMock())

    async def fresh_run(db: object, run_id: object) -> SimpleNamespace:
        del db, run_id
        return _run()

    monkeypatch.setattr("app.runs.service_lifecycle._get_run_for_update", fresh_run)

    release = AsyncMock()
    release.lock_run_devices = AsyncMock(return_value={})
    release.clear_desired_grid_run_id_for_run = AsyncMock()
    release.release_devices = AsyncMock(side_effect=_other_db_error())
    release.complete_deferred_stops_post_commit = AsyncMock()

    lifecycle = _make_lifecycle(release, _FakeSessionFactory())

    with pytest.raises(DBAPIError):
        await lifecycle.cancel_run(uuid.uuid4())

    assert release.release_devices.await_count == 1, "only deadlocks are retried"
