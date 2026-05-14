import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.devices.models import DeviceOperationalState
from app.sessions import service_sync as session_sync
from app.sessions.models import SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME


def test_extract_sessions_from_grid_filters_invalid_reserved_and_probe_sessions() -> None:
    assert session_sync._extract_sessions_from_grid({"value": "bad"}) == {}
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
                                        "appium:udid": "target",
                                        "gridfleet:deviceId": "device-id",
                                        "gridfleet:testName": "smoke",
                                    },
                                }
                            },
                        ]
                    }
                ]
            }
        }
    )

    assert result == {
        "real": {
            "connection_target": "target",
            "device_id": "device-id",
            "test_name": "smoke",
            "requested_capabilities": {
                "appium:udid": "target",
                "gridfleet:deviceId": "device-id",
                "gridfleet:testName": "smoke",
            },
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
    monkeypatch.setattr(session_sync.lifecycle_policy, "complete_deferred_stop_if_session_ended", complete)

    await session_sync._sweep_stale_stop_pending(db)

    complete.assert_awaited_once_with(db, device)


async def test_sync_sessions_unreachable_grid_still_sweeps(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.commit = AsyncMock()
    monkeypatch.setattr(
        session_sync.grid_service,
        "get_grid_status",
        AsyncMock(return_value={"value": {"ready": False}, "error": "down"}),
    )
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    sweep = AsyncMock()
    monkeypatch.setattr(session_sync, "_sweep_stale_stop_pending", sweep)

    await session_sync._sync_sessions(db)

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
    ]
    db.execute = AsyncMock(side_effect=execute_results)
    monkeypatch.setattr(
        session_sync.grid_service, "get_grid_status", AsyncMock(return_value={"value": {"ready": True, "nodes": []}})
    )
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(
        session_sync.lifecycle_policy,
        "handle_session_finished",
        AsyncMock(return_value=session_sync.lifecycle_policy.DeferredStopOutcome.NO_PENDING),
    )
    monkeypatch.setattr(
        session_sync.device_locking,
        "lock_device",
        AsyncMock(return_value=SimpleNamespace(id=device_id, operational_state=DeviceOperationalState.busy)),
    )
    monkeypatch.setattr(session_sync, "ready_operational_state", AsyncMock(return_value=DeviceOperationalState.offline))
    machine = SimpleNamespace(transition=AsyncMock())
    monkeypatch.setattr(session_sync, "_MACHINE", machine)
    monkeypatch.setattr(session_sync, "_sweep_stale_stop_pending", AsyncMock())
    monkeypatch.setattr(session_sync.session_service, "queue_session_ended_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(session_sync, "revoke_intents_and_reconcile", AsyncMock())

    await session_sync._sync_sessions(db)

    machine.transition.assert_awaited_once()
    assert machine.transition.await_args.args[1] is session_sync.TransitionEvent.AUTO_STOP_EXECUTED


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
    monkeypatch.setattr(
        session_sync.grid_service,
        "get_grid_status",
        AsyncMock(
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
        ),
    )
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(
        session_sync.run_service, "get_device_reservation_with_entry", AsyncMock(return_value=(None, None))
    )
    monkeypatch.setattr(session_sync, "_sweep_stale_stop_pending", AsyncMock())

    await session_sync._sync_sessions(db)

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
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(scalar_one_or_none=None),
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[1], operational_state=DeviceOperationalState.busy)
            ),
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[2], operational_state=DeviceOperationalState.busy)
            ),
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[3], operational_state=DeviceOperationalState.available)
            ),
            _ExecuteResult(scalars_first=None),
            _ExecuteResult(
                scalar_one_or_none=SimpleNamespace(id=device_ids[4], operational_state=DeviceOperationalState.busy)
            ),
        ]
    )
    monkeypatch.setattr(
        session_sync.grid_service,
        "get_grid_status",
        AsyncMock(return_value={"value": {"ready": True, "nodes": []}}),
    )
    monkeypatch.setattr(session_sync, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(session_sync.session_service, "queue_session_ended_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(session_sync, "revoke_intents_and_reconcile", AsyncMock())
    monkeypatch.setattr(
        session_sync.lifecycle_policy,
        "handle_session_finished",
        AsyncMock(
            side_effect=[
                session_sync.lifecycle_policy.DeferredStopOutcome.AUTO_STOPPED,
                session_sync.lifecycle_policy.DeferredStopOutcome.RUNNING_SESSION_EXISTS,
                session_sync.lifecycle_policy.DeferredStopOutcome.NO_PENDING,
                session_sync.lifecycle_policy.DeferredStopOutcome.NO_PENDING,
            ]
        ),
    )
    monkeypatch.setattr(
        session_sync.device_locking,
        "lock_device",
        AsyncMock(return_value=SimpleNamespace(id=device_ids[4], operational_state=DeviceOperationalState.available)),
    )
    monkeypatch.setattr(session_sync, "_sweep_stale_stop_pending", AsyncMock())

    await session_sync._sync_sessions(db)

    assert session_sync.lifecycle_policy.handle_session_finished.await_count == 4
    session_sync.device_locking.lock_device.assert_awaited_once()
