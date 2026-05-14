import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler_convergence import DesiredRow
from app.hosts.models import HostStatus


async def test_fetch_backoff_until_parses_valid_rows_and_skips_bad_values() -> None:
    valid_id = uuid.uuid4()
    naive_id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock(
        return_value=SimpleNamespace(
            all=lambda: [
                (uuid.uuid4(), None),
                (uuid.uuid4(), {"backoff_until": 123}),
                (uuid.uuid4(), {"backoff_until": "not-a-date"}),
                (valid_id, {"backoff_until": "2026-05-13T12:00:00+00:00"}),
                (naive_id, {"backoff_until": "2026-05-13T12:00:00"}),
            ]
        )
    )

    backoff = await appium_reconciler._fetch_backoff_until(db)

    assert backoff[valid_id] == datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    assert backoff[naive_id] == datetime(2026, 5, 13, 12, 0, tzinfo=UTC)


async def test_converge_device_now_return_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    db = MagicMock()
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=None))
    assert await appium_reconciler.converge_device_now(device_id, db=db) is None

    row = SimpleNamespace(device_id=device_id, host_id=uuid.uuid4(), node_id=uuid.uuid4())
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=row))
    db.get = AsyncMock(return_value=None)
    assert await appium_reconciler.converge_device_now(device_id, db=db) is None

    host = SimpleNamespace(id=row.host_id, status=HostStatus.online, ip="10.0.0.1", agent_port=5100)
    db.get = AsyncMock(side_effect=[host])
    monkeypatch.setattr(appium_reconciler, "agent_health", AsyncMock(return_value={"status": "ok"}))
    assert await appium_reconciler.converge_device_now(device_id, db=db) is None

    node = SimpleNamespace(id=row.node_id)
    db.get = AsyncMock(side_effect=[host, node])
    db.refresh = AsyncMock()
    monkeypatch.setattr(
        appium_reconciler,
        "agent_health",
        AsyncMock(
            return_value={
                "appium_processes": {"running_nodes": [{"port": 4723, "pid": 123, "connection_target": "dev"}]}
            }
        ),
    )
    converge = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "converge_host_rows", converge)

    assert await appium_reconciler.converge_device_now(device_id, db=db) is node
    converge.assert_awaited_once()
    db.refresh.assert_awaited_once_with(node)


async def test_write_observed_factory_running_and_stopped_clear_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    db = Session()
    device_id = uuid.uuid4()
    row = DesiredRow(
        device_id=device_id,
        host_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        connection_target="dev",
        desired_state="running",
        desired_port=4723,
        transition_token=uuid.uuid4(),
        transition_deadline=None,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )
    node = SimpleNamespace(
        desired_state="running", desired_port=4723, transition_token=row.transition_token, transition_deadline=None
    )
    device = SimpleNamespace(id=device_id, appium_node=node)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=device))
    monkeypatch.setattr(appium_reconciler, "mark_node_started", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "mark_node_stopped", AsyncMock())
    write = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "write_desired_state", write)

    observed = appium_reconciler._write_observed_factory(require_leader=False, session_scope=lambda: db)
    await observed(
        row=row,
        state="running",
        port=None,
        pid=123,
        active_connection_target="dev",
        clear_desired_port=True,
        allocated_caps={"x": "y"},
    )
    appium_reconciler.mark_node_started.assert_awaited_once()

    await observed(
        row=row,
        state="stopped",
        port=None,
        pid=None,
        active_connection_target=None,
        clear_desired_port=True,
        clear_transition=True,
    )
    appium_reconciler.mark_node_stopped.assert_awaited_once()
    assert write.await_args.kwargs["desired_port"] is None
    assert write.await_args.kwargs["transition_token"] is None


async def test_write_observed_and_clear_factories_handle_missing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    db = Session()
    row = DesiredRow(
        device_id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        connection_target="dev",
        desired_state="running",
        desired_port=4723,
        transition_token=uuid.uuid4(),
        transition_deadline=None,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=None))
    monkeypatch.setattr(appium_reconciler, "mark_node_started", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "mark_node_stopped", AsyncMock())
    observed = appium_reconciler._write_observed_factory(require_leader=False, session_scope=lambda: db)
    await observed(row=row, state="running", port=4723, pid=1, active_connection_target="dev")

    device = SimpleNamespace(id=row.device_id, appium_node=None)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=device))
    await observed(row=row, state="running", port=4723, pid=1, active_connection_target="dev", clear_desired_port=True)

    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=None))
    await observed(row=row, state="stopped", port=None, pid=None, active_connection_target=None, clear_transition=True)

    clear_token = appium_reconciler._clear_token_factory(require_leader=False, session_scope=lambda: db)
    monkeypatch.setattr(appium_reconciler, "_clear_transition_token", AsyncMock())
    await clear_token(row=row, reason="done")
    appium_reconciler._clear_transition_token.assert_awaited_once_with(db, row)


async def test_session_scope_reuses_existing_db() -> None:
    db = object()
    async with appium_reconciler._session_scope(db)() as yielded:
        assert yielded is db


async def test_reconcile_all_stop_callback_raises_for_agent_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    host_id = uuid.uuid4()

    async def fake_reconcile_host_orphans(**kwargs: object) -> list[object]:
        stop_agent = kwargs["appium_stop"]
        await stop_agent(host="10.0.0.1", agent_port=5100, port=4723)
        return []

    response = MagicMock()
    response.raise_for_status.side_effect = RuntimeError("stop failed")
    monkeypatch.setattr(appium_reconciler, "reconcile_host_orphans", fake_reconcile_host_orphans)
    monkeypatch.setattr(appium_reconciler, "appium_stop", AsyncMock(return_value=response))

    assert (
        await appium_reconciler._reconcile_all(
            [{"id": host_id, "ip": "10.0.0.1", "agent_port": 5100}],
            [{"host_id": host_id, "device_connection_target": "dev", "node_port": 4723}],
        )
        == {}
    )
    response.raise_for_status.assert_called_once()


async def test_reconciler_loop_logs_unexpected_cycle_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class Cycle:
        def cycle(self) -> "Cycle":
            return self

        async def __aenter__(self) -> "Cycle":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(appium_reconciler.settings_service, "get", lambda key: 0.01)
    monkeypatch.setattr(appium_reconciler, "observe_background_loop", lambda *args, **kwargs: Cycle())
    monkeypatch.setattr(appium_reconciler, "async_session", lambda: Session())
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(appium_reconciler.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))

    with pytest.raises(asyncio.CancelledError):
        await appium_reconciler.appium_reconciler_loop()


async def test_drive_convergence_return_paths_and_cycle_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    row = DesiredRow(
        device_id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        connection_target="dev",
        desired_state="running",
        desired_port=4723,
        transition_token=None,
        transition_deadline=None,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )
    monkeypatch.setattr(appium_reconciler.settings_service, "get", lambda key: 1)
    monkeypatch.setattr(appium_reconciler, "async_session", lambda: Session())
    monkeypatch.setattr(appium_reconciler, "assert_current_leader", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "agent_health", AsyncMock(return_value={"appium_processes": "bad"}))
    monkeypatch.setattr(appium_reconciler, "_touch_last_observed", AsyncMock())
    converge = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "converge_host_rows", converge)

    await appium_reconciler._drive_convergence(
        [{"id": row.host_id, "ip": "10.0.0.1", "agent_port": 5100}, {"id": "bad"}],
        [row],
        {},
    )
    converge.assert_not_awaited()

    monkeypatch.setattr(appium_reconciler, "_fetch_online_hosts", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_node_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(appium_reconciler, "_fetch_backoff_until", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "_reconcile_all", AsyncMock(return_value={}))
    monkeypatch.setattr(appium_reconciler, "reconciler_convergence_enabled", lambda: True)
    monkeypatch.setattr(appium_reconciler, "_drive_convergence", AsyncMock())

    await appium_reconciler.run_one_cycle_for_test()

    appium_reconciler._drive_convergence.assert_awaited_once()
