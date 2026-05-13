import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.host import HostStatus
from app.services import appium_reconciler
from app.services.appium_reconciler_convergence import DesiredRow


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
