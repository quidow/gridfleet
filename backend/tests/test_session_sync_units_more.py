import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.device import DeviceOperationalState
from app.models.session import SessionStatus
from app.services import session_sync
from app.services.session_probe_constants import PROBE_TEST_NAME


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
