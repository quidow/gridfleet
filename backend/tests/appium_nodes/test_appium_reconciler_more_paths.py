import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_convergence import DesiredRow
from app.hosts.models import HostStatus
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    import pytest


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

    backoff = await appium_reconciler.fetch_backoff_until(db)

    assert backoff[valid_id] == datetime(2026, 5, 13, 12, 0, tzinfo=UTC)
    assert backoff[naive_id] == datetime(2026, 5, 13, 12, 0, tzinfo=UTC)


async def test_converge_device_now_return_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    db = MagicMock()
    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=None,
        circuit_breaker=Mock(),
        session_factory=AsyncMock(),
    )

    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=None))
    assert await svc.converge_device_now(device_id, db=db) is None

    row = SimpleNamespace(device_id=device_id, host_id=uuid.uuid4(), node_id=uuid.uuid4())
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=row))
    db.get = AsyncMock(return_value=None)
    assert await svc.converge_device_now(device_id, db=db) is None


async def test_converge_device_now_pokes_agent_without_agent_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe-only convergence: no ``converge_host_rows`` I/O — fire a wake
    poke and return the node row."""
    device_id = uuid.uuid4()
    db = MagicMock()
    settings = FakeSettingsReader({})
    circuit_breaker = Mock()
    svc = ReconcilerService(
        publisher=Mock(),
        settings=settings,
        pool=None,
        circuit_breaker=circuit_breaker,
        session_factory=AsyncMock(),
    )
    row = SimpleNamespace(device_id=device_id, host_id=uuid.uuid4(), node_id=uuid.uuid4())
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=row))
    host = SimpleNamespace(
        id=row.host_id,
        status=HostStatus.online,
        ip="10.0.0.9",
        agent_port=5100,
        capabilities=None,
    )
    node = SimpleNamespace(id=row.node_id)
    db.get = AsyncMock(side_effect=[host, node])
    db.refresh = AsyncMock()
    converge = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "converge_host_rows", converge)
    poke = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "agent_nodes_refresh", poke)

    assert await svc.converge_device_now(device_id, db=db) is node

    poke.assert_awaited_once_with(
        host.ip, host.agent_port, settings=settings, pool=None, circuit_breaker=circuit_breaker
    )
    converge.assert_not_awaited()
    db.refresh.assert_awaited_once_with(node)


async def test_write_observed_factory_running_and_stopped_clear_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        async def __aenter__(self) -> Session:
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

    observed = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=Mock(),
    )._write_observed_factory(session_scope=lambda: db)
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
    written = write.await_args.kwargs["write"]
    assert written.desired_port is None
    assert written.transition_token is None


async def test_write_observed_and_clear_factories_handle_missing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    class Session:
        async def __aenter__(self) -> Session:
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
    _reconciler_svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=Mock(),
    )
    observed = _reconciler_svc._write_observed_factory(session_scope=lambda: db)
    await observed(row=row, state="running", port=4723, pid=1, active_connection_target="dev")

    device = SimpleNamespace(id=row.device_id, appium_node=None)
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=device))
    await observed(row=row, state="running", port=4723, pid=1, active_connection_target="dev", clear_desired_port=True)

    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=None))
    await observed(row=row, state="stopped", port=None, pid=None, active_connection_target=None, clear_transition=True)

    clear_token = _reconciler_svc._clear_token_factory(session_scope=lambda: db)
    monkeypatch.setattr(appium_reconciler, "_clear_transition_token", AsyncMock())
    await clear_token(row=row)
    appium_reconciler._clear_transition_token.assert_awaited_once_with(db, row)


async def test_session_scope_reuses_existing_db() -> None:
    db = object()
    async with appium_reconciler._session_scope(db)() as yielded:
        assert yielded is db


async def test_reconcile_host_returns_for_malformed_appium_processes(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr(appium_reconciler, "_touch_last_observed", AsyncMock())
    converge = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "converge_host_rows", converge)

    @asynccontextmanager
    async def _mock_session_factory() -> AsyncMock:
        yield AsyncMock()

    service = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_mock_session_factory,
    )
    await service.reconcile_host(
        host_id=row.host_id,
        host_ip="10.0.0.1",
        agent_port=5100,
        rows=[row],
        backoff_until_by_device={},
        payload={"appium_processes": "bad"},
    )
    converge.assert_not_awaited()
