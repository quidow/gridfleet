import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_agent import NodeStartDetails
from app.appium_nodes.services.reconciler_convergence import DesiredRow
from app.hosts.models import HostStatus
from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest


async def test_converge_device_now_return_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    factory = _FakeSessionFactory()
    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=None,
        circuit_breaker=Mock(),
        session_factory=factory,  # type: ignore[arg-type]
        incidents=LifecycleIncidentService(),
    )

    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=None))
    assert await svc.converge_device_now(device_id) is None

    row = SimpleNamespace(device_id=device_id, host_id=uuid.uuid4(), node_id=uuid.uuid4())
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=row))
    factory.session.get = AsyncMock(return_value=None)
    assert await svc.converge_device_now(device_id) is None


async def test_converge_device_now_pokes_agent_without_agent_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Observe-only convergence: no ``converge_host_rows`` I/O — fire a wake
    poke, once the read session that found the host has already closed."""
    device_id = uuid.uuid4()
    factory = _FakeSessionFactory()
    settings = FakeSettingsReader({})
    circuit_breaker = Mock()
    svc = ReconcilerService(
        publisher=Mock(),
        settings=settings,
        pool=None,
        circuit_breaker=circuit_breaker,
        session_factory=factory,  # type: ignore[arg-type]
        incidents=LifecycleIncidentService(),
    )
    row = SimpleNamespace(device_id=device_id, host_id=uuid.uuid4(), node_id=uuid.uuid4())
    monkeypatch.setattr(appium_reconciler, "_fetch_desired_row", AsyncMock(return_value=row))
    host = SimpleNamespace(
        id=row.host_id,
        status=HostStatus.online,
        last_heartbeat=datetime.now(UTC),
        ip="10.0.0.9",
        agent_port=5100,
        capabilities=None,
    )
    factory.session.get = AsyncMock(return_value=host)
    converge = AsyncMock()
    monkeypatch.setattr(ReconcilerService, "converge_host_rows", converge)
    poke = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "poke_node_refresh_target", poke)

    assert await svc.converge_device_now(device_id) is None

    poke.assert_awaited_once_with(
        appium_reconciler.NodeRefreshTarget(ip=host.ip, agent_port=host.agent_port),
        circuit_breaker=circuit_breaker,
        pool=None,
    )
    converge.assert_not_awaited()


class _FakeSessionFactory:
    """Minimal ``SessionFactory`` stand-in whose ``begin()`` yields a mock session."""

    def __init__(self) -> None:
        self.session = AsyncMock()

    def __call__(self) -> object:
        return self._scope()

    def begin(self) -> object:
        return self._scope()

    @asynccontextmanager
    async def _scope(self) -> AsyncIterator[AsyncMock]:
        yield self.session


def _observed_row(device_id: uuid.UUID) -> DesiredRow:
    return DesiredRow(
        device_id=device_id,
        host_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        connection_target="dev",
        desired_state="running",
        desired_port=4723,
        port=4723,
        pid=None,
        active_connection_target=None,
        stop_pending=False,
    )


def _service(session_factory: object) -> ReconcilerService:
    return ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=session_factory,  # type: ignore[arg-type]
        incidents=LifecycleIncidentService(),
    )


async def test_write_observed_factory_running_and_stopped_clear_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    device_id = uuid.uuid4()
    row = _observed_row(device_id)
    node = SimpleNamespace(desired_state="running", desired_port=4723, restart_requested_at=None)
    device = SimpleNamespace(id=device_id, appium_node=node)
    monkeypatch.setattr(
        appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=SimpleNamespace(device=device))
    )
    monkeypatch.setattr(appium_reconciler, "load_device_decision_snapshot", AsyncMock(return_value=object()))
    monkeypatch.setattr(appium_reconciler, "lock_appium_node_for_device", AsyncMock(return_value=node))
    monkeypatch.setattr(appium_reconciler, "mark_node_started", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "mark_node_stopped", AsyncMock())
    write = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "write_desired_state", write)

    observed = _service(_FakeSessionFactory())._write_observed_factory()
    await observed(
        row=row,
        state="running",
        port=None,
        pid=123,
        details=NodeStartDetails(active_connection_target="dev", allocated_caps={"x": "y"}),
        clear_desired_port=True,
    )
    appium_reconciler.mark_node_started.assert_awaited_once()

    await observed(
        row=row,
        state="stopped",
        port=None,
        pid=None,
        clear_desired_port=True,
    )
    appium_reconciler.mark_node_stopped.assert_awaited_once()
    written = write.await_args.kwargs["write"]
    assert written.desired_port is None
    assert written.restart_requested_at is None


async def test_write_observed_and_clear_factories_handle_missing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _observed_row(uuid.uuid4())
    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=None))
    monkeypatch.setattr(appium_reconciler, "load_device_decision_snapshot", AsyncMock(return_value=object()))
    monkeypatch.setattr(appium_reconciler, "mark_node_started", AsyncMock())
    monkeypatch.setattr(appium_reconciler, "mark_node_stopped", AsyncMock())
    write = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "write_desired_state", write)

    observed = _service(_FakeSessionFactory())._write_observed_factory()
    # Deleted device: the command declines before locking the node.
    await observed(row=row, state="running", port=4723, pid=1, details=NodeStartDetails(active_connection_target="dev"))
    appium_reconciler.mark_node_started.assert_not_awaited()

    # Device present but its node row went away: mark_* runs with locked_node None
    # and the desired-port clear is skipped rather than raising.
    device = SimpleNamespace(id=row.device_id, appium_node=None)
    monkeypatch.setattr(
        appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=SimpleNamespace(device=device))
    )
    monkeypatch.setattr(appium_reconciler, "lock_appium_node_for_device", AsyncMock(return_value=None))
    await observed(
        row=row,
        state="running",
        port=4723,
        pid=1,
        details=NodeStartDetails(active_connection_target="dev"),
        clear_desired_port=True,
    )
    appium_reconciler.mark_node_started.assert_awaited_once()
    write.assert_not_awaited()

    await observed(row=row, state="stopped", port=None, pid=None, clear_desired_port=True)
    appium_reconciler.mark_node_stopped.assert_awaited_once()
    write.assert_not_awaited()


async def test_reconcile_host_returns_for_malformed_appium_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    row = DesiredRow(
        device_id=uuid.uuid4(),
        host_id=uuid.uuid4(),
        node_id=uuid.uuid4(),
        connection_target="dev",
        desired_state="running",
        desired_port=4723,
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
        incidents=LifecycleIncidentService(),
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
