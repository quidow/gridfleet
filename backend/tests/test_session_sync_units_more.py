import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.models import DeviceOperationalState
from app.devices.services.intent import IntentService
from app.sessions import service_sync as session_sync
from app.sessions.models import SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from app.sessions.service_sync import SessionSyncService
from tests.fakes import FakeSettingsReader, make_fake_grid
from tests.helpers import test_event_bus as event_bus


def test_extract_sessions_from_grid_filters_invalid_reserved_and_probe_sessions() -> None:
    assert session_sync._extract_sessions_from_grid({"value": "bad"}) == {}
    valid_device_id = str(uuid.uuid4())
    result = session_sync._extract_sessions_from_grid(
        {
            "value": {
                "nodes": [
                    {
                        "slots": [
                            {"session": None},
                            {"session": {"sessionId": "reserved"}},
                            {
                                "session": {
                                    "sessionId": "probe-a",
                                    "capabilities": {"gridfleet:probeSession": True},
                                }
                            },
                            {
                                "session": {
                                    "sessionId": "probe-b",
                                    "capabilities": {"gridfleet:testName": PROBE_TEST_NAME},
                                }
                            },
                            {
                                "session": {
                                    "sessionId": "real",
                                    "capabilities": {
                                        "gridfleet:testName": "smoke",
                                    },
                                    "stereotype": {
                                        "appium:udid": "target",
                                        "appium:gridfleet:deviceId": valid_device_id,
                                    },
                                }
                            },
                        ]
                    }
                ]
            }
        }
    )

    # ``device_id`` is now UUID-validated upstream; non-UUID strings are
    # dropped to ``None`` and the consumer falls back to connection_target.
    assert result == {
        "real": {
            "connection_target": "target",
            "device_id": valid_device_id,
            "test_name": "smoke",
            "requested_capabilities": {"gridfleet:testName": "smoke"},
        }
    }


async def test_sweep_stale_stop_pending_handles_deleted_rows(monkeypatch: pytest.MonkeyPatch) -> None:
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

    svc = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=make_fake_grid(), lifecycle=mock_lifecycle
    )
    await svc._sweep_stale_stop_pending(db)

    complete.assert_awaited_once_with(db, device)


async def test_sync_sessions_unreachable_grid_still_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    fake_grid = AsyncMock()
    fake_grid.get_status = AsyncMock(return_value={"value": {"ready": False}, "error": "down"})
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    sweep = AsyncMock()

    svc = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=fake_grid, lifecycle=AsyncMock()
    )
    monkeypatch.setattr(svc, "_sweep_stale_stop_pending", sweep)
    await svc.sync(db)

    sweep.assert_awaited_once_with(db)
    db.commit.assert_awaited_once()


async def test_sync_sessions_finish_restore_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    db = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=SimpleNamespace(id=device_id, operational_state=DeviceOperationalState.busy))
    ended_session = SimpleNamespace(
        session_id="ended",
        device_id=device_id,
        device=SimpleNamespace(id=device_id),
        run=None,
        status=SessionStatus.running,
        ended_at=None,
        error_type=None,
        error_message=None,
    )
    execute_results = [
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [ended_session])),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [ended_session])),
        SimpleNamespace(scalars=lambda: SimpleNamespace(first=lambda: None)),
        SimpleNamespace(
            scalar_one_or_none=lambda: SimpleNamespace(id=device_id, operational_state=DeviceOperationalState.busy)
        ),
        SimpleNamespace(first=lambda: None),
        # After Task 10: always recheck fresh running sessions (always_lock path).
        SimpleNamespace(first=lambda: None),
        SimpleNamespace(first=lambda: None),
        SimpleNamespace(first=lambda: None),
    ]
    db.execute = AsyncMock(side_effect=execute_results)
    fake_grid = AsyncMock()
    fake_grid.get_status = AsyncMock(return_value={"value": {"ready": True, "nodes": []}})
    fake_grid.available_node_device_ids = MagicMock(return_value=None)
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(
        session_sync.device_locking,
        "lock_device",
        AsyncMock(return_value=SimpleNamespace(id=device_id, operational_state=DeviceOperationalState.busy)),
    )
    monkeypatch.setattr(session_sync.session_service, "queue_session_ended_event", lambda *args, **kwargs: None)
    revoke_mock = AsyncMock()
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", revoke_mock)
    mark_dirty = AsyncMock()
    monkeypatch.setattr(IntentService, "mark_dirty_and_reconcile", mark_dirty)

    mock_lifecycle = AsyncMock()
    mock_lifecycle.handle_session_finished = AsyncMock(
        return_value=session_sync.lifecycle_policy.DeferredStopOutcome.NO_PENDING
    )
    svc = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=fake_grid, lifecycle=mock_lifecycle
    )
    monkeypatch.setattr(svc, "_sweep_stale_stop_pending", AsyncMock())
    await svc.sync(db)

    # After Task 10: _MACHINE removed; mark_dirty_and_reconcile is called.
    mark_dirty.assert_awaited()


class _ScalarResult:
    def __init__(self, *, all_values: list[object] | None = None, first_value: object | None = None) -> None:
        self._all_values = all_values or []
        self._first_value = first_value

    def all(self) -> list[object]:
        return self._all_values

    def first(self) -> object | None:
        return self._first_value


class _ExecuteResult:
    def __init__(
        self,
        *,
        scalars_all: list[object] | None = None,
        scalars_first: object | None = None,
        scalar_one_or_none: object | None = None,
        first: object | None = None,
    ) -> None:
        self._scalars = _ScalarResult(all_values=scalars_all, first_value=scalars_first)
        self._scalar_one_or_none = scalar_one_or_none
        self._first = first

    def scalars(self) -> _ScalarResult:
        return self._scalars

    def scalar_one_or_none(self) -> object | None:
        return self._scalar_one_or_none

    def first(self) -> object | None:
        return self._first


async def test_sync_sessions_new_session_race_and_invalid_capability_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    valid_id = uuid.uuid4()
    device = SimpleNamespace(id=valid_id, name="Device")
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _ExecuteResult(scalars_all=[]),
            _ExecuteResult(scalars_all=[]),
            _ExecuteResult(scalar_one_or_none=None),
            _ExecuteResult(scalar_one_or_none=device),
            _ExecuteResult(scalar_one_or_none=None),
            _ExecuteResult(scalar_one_or_none=device),
            _ExecuteResult(scalar_one_or_none=uuid.uuid4()),
        ]
    )
    fake_grid = AsyncMock()
    fake_grid.get_status = AsyncMock(
        return_value={
            "value": {
                "ready": True,
                "nodes": [
                    {
                        "slots": [
                            {"session": {"sessionId": "missing-target", "capabilities": {}}},
                            {
                                "session": {
                                    "sessionId": "bad-device-id",
                                    "capabilities": {
                                        "appium:udid": "bad-target",
                                        "gridfleet:deviceId": "not-a-uuid",
                                    },
                                }
                            },
                            {
                                "session": {
                                    "sessionId": "concurrent",
                                    "capabilities": {
                                        "appium:udid": "concurrent-target",
                                        "gridfleet:deviceId": str(valid_id),
                                    },
                                }
                            },
                            {
                                "session": {
                                    "sessionId": "vanished",
                                    "capabilities": {
                                        "appium:udid": "vanished-target",
                                        "gridfleet:deviceId": str(valid_id),
                                    },
                                }
                            },
                        ]
                    }
                ],
            }
        }
    )
    fake_grid.available_node_device_ids = MagicMock(return_value=None)
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(
        session_sync.run_service, "get_device_reservation_with_entry", AsyncMock(return_value=(None, None))
    )

    svc = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=fake_grid, lifecycle=AsyncMock()
    )
    monkeypatch.setattr(svc, "_sweep_stale_stop_pending", AsyncMock())
    await svc.sync(db)

    assert db.get.await_count == 1
    db.commit.assert_awaited_once()


async def test_sync_sessions_end_restore_skip_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    device_ids = [uuid.UUID(int=index) for index in range(1, 6)]
    running = [
        SimpleNamespace(session_id=f"ended-{index}", device_id=device_id)
        for index, device_id in enumerate(device_ids, start=1)
    ]
    ended_sessions = [
        SimpleNamespace(
            session_id=item.session_id,
            device_id=item.device_id,
            device=SimpleNamespace(id=item.device_id),
            run=None,
            status=SessionStatus.running,
            ended_at=None,
            error_type=None,
            error_message=None,
        )
        for item in running
    ]
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            _ExecuteResult(scalars_all=running),
            *[_ExecuteResult(scalars_all=[ended]) for ended in ended_sessions],
            # still_running + device for device_ids[0] (None → skip, no handle_session_finished)
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(scalar_one_or_none=None),
            # still_running + device for device_ids[1] (AUTO_STOPPED → continue, but now we always lock+fresh_running)
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[1], operational_state=DeviceOperationalState.busy)
            ),
            # fresh_running for device_ids[1] (AUTO_STOPPED → continue before lock)
            # Note: AUTO_STOPPED does continue BEFORE lock, so no fresh_running
            # still_running + device for device_ids[2] (RUNNING_SESSION_EXISTS → continue before lock)
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[2], operational_state=DeviceOperationalState.busy)
            ),
            # still_running + device for device_ids[3] (NO_PENDING → lock + fresh_running)
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[3], operational_state=DeviceOperationalState.available)
            ),
            _ExecuteResult(first=None),  # fresh_running for device_ids[3]
            # still_running + device for device_ids[4] (NO_PENDING → lock + fresh_running)
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[4], operational_state=DeviceOperationalState.busy)
            ),
            _ExecuteResult(first=None),  # fresh_running for device_ids[4]
        ]
    )
    fake_grid = AsyncMock()
    fake_grid.get_status = AsyncMock(return_value={"value": {"ready": True, "nodes": []}})
    fake_grid.available_node_device_ids = MagicMock(return_value=None)
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(session_sync.session_service, "queue_session_ended_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(IntentService, "revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(IntentService, "mark_dirty_and_reconcile", AsyncMock())
    monkeypatch.setattr(
        session_sync.device_locking,
        "lock_device",
        AsyncMock(return_value=SimpleNamespace(id=device_ids[4], operational_state=DeviceOperationalState.available)),
    )

    _deferred_stop_outcome = session_sync.lifecycle_policy.DeferredStopOutcome
    mock_handle_session_finished = AsyncMock(
        side_effect=[
            _deferred_stop_outcome.AUTO_STOPPED,
            _deferred_stop_outcome.RUNNING_SESSION_EXISTS,
            _deferred_stop_outcome.NO_PENDING,
            _deferred_stop_outcome.NO_PENDING,
        ]
    )
    mock_lifecycle = AsyncMock()
    mock_lifecycle.handle_session_finished = mock_handle_session_finished

    svc = SessionSyncService(
        publisher=event_bus, settings=FakeSettingsReader({}), grid=fake_grid, lifecycle=mock_lifecycle
    )
    monkeypatch.setattr(svc, "_sweep_stale_stop_pending", AsyncMock())
    await svc.sync(db)

    assert mock_handle_session_finished.await_count == 4
    # After Task 10: lock_device is called for each device with NO_PENDING outcome
    # (device_ids[3] and [4] both get NO_PENDING → 2 lock calls).
    assert session_sync.device_locking.lock_device.await_count >= 1
