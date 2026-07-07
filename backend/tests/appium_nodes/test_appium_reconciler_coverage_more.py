import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import httpx2 as httpx
import pytest
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.exceptions import NodeAlreadyRunningError, NodeStopNotAcknowledgedError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_convergence import DesiredRow, ObservedEntry
from app.devices.models import DeviceOperationalState
from app.hosts.models import Host, HostStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def _desired_row(**overrides: object) -> DesiredRow:
    values: dict[str, object] = {
        "device_id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "connection_target": "serial-1",
        "desired_state": "running",
        "desired_port": 4723,
        "transition_token": None,
        "transition_deadline": None,
        "port": None,
        "pid": None,
        "active_connection_target": None,
        "stop_pending": False,
    }
    values.update(overrides)
    return DesiredRow(**values)  # type: ignore[arg-type]


async def test_lock_device_for_reconciler_returns_none_when_row_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        appium_reconciler.device_locking,
        "lock_device",
        AsyncMock(side_effect=NoResultFound),
    )
    result = await appium_reconciler._lock_device_for_reconciler(AsyncMock(), uuid.uuid4())
    assert result is None


async def test_appium_reconciler_fetches_db_rows_and_backoff(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    db_host.status = HostStatus.online
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Reconciler Device",
        identity_value="reconciler-001",
        connection_target="reconciler-target",
        operational_state=DeviceOperationalState.available,
        lifecycle_policy_state={
            "backoff_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
        },
    )
    token = uuid.uuid4()
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        transition_token=token,
        transition_deadline=deadline,
        pid=123,
        active_connection_target="reconciler-target",
        stop_pending=True,
    )
    db_session.add(node)
    await db_session.commit()

    desired = await appium_reconciler.fetch_desired_rows(db_session)
    desired_one = await appium_reconciler._fetch_desired_row(db_session, device.id)
    missing = await appium_reconciler._fetch_desired_row(db_session, uuid.uuid4())
    backoff = await appium_reconciler.fetch_backoff_until(db_session)

    assert desired[0].transition_token == token
    assert desired_one is not None
    assert desired_one.stop_pending is True
    assert missing is None
    assert device.id in backoff

    device.lifecycle_policy_state = {"backoff_until": "not-a-date"}
    await db_session.commit()
    assert await appium_reconciler.fetch_backoff_until(db_session) == {}


async def test_reconcile_host_filters_backoff_rows_from_explicit_health_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = uuid.uuid4()
    active = _desired_row(host_id=host_id)
    backed_off = _desired_row(
        host_id=host_id,
        device_id=uuid.uuid4(),
        connection_target="serial-2",
    )
    observed_payload = {
        "appium_processes": {
            "running_nodes": [
                {"port": 4723, "pid": 10, "connection_target": active.connection_target, "platform_id": "android"}
            ]
        }
    }
    touch = AsyncMock()
    converge = AsyncMock()
    monkeypatch.setattr("app.appium_nodes.services.reconciler._touch_last_observed", touch)
    monkeypatch.setattr(ReconcilerService, "converge_host_rows", converge)

    @asynccontextmanager
    async def _mock_session_factory() -> AsyncMock:
        yield AsyncMock()

    svc = ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=_mock_session_factory,
    )
    await svc.reconcile_host(
        host_id=host_id,
        host_ip="10.0.0.3",
        agent_port=5100,
        rows=[active, backed_off],
        backoff_until_by_device={backed_off.device_id: datetime.now(UTC) + timedelta(minutes=10)},
        payload=observed_payload,
    )

    touch.assert_awaited_once()
    converge.assert_awaited_once()
    # desired_rows (active_rows) is 2nd positional arg to converge_host_rows(None, active_rows, observed, ...)
    assert converge.call_args.args[1] == [active]


async def test_stop_agent_factory_and_start_failure_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _desired_row()
    svc = ReconcilerService(
        publisher=Mock(), settings=FakeSettingsReader(), pool=Mock(), circuit_breaker=Mock(), session_factory=Mock()
    )
    stop_agent = svc._make_stop_agent("10.0.0.1", 5100)
    assert await stop_agent(row=row, port=None) is None
    assert await stop_agent(row=row, port=0) is None

    monkeypatch.setattr("app.appium_nodes.services.reconciler.stop_remote_node", AsyncMock(return_value=False))
    with pytest.raises(NodeStopNotAcknowledgedError, match="did not acknowledge"):
        await stop_agent(row=row, port=4723)

    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler.stop_remote_node", AsyncMock(side_effect=httpx.ConnectError("down"))
    )
    with pytest.raises(httpx.ConnectError):
        await stop_agent(row=row, port=4723)


async def test_start_agent_does_not_record_failure_on_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """A per-target ALREADY_RUNNING from the agent is not a start failure: the
    ``_start`` closure must re-raise it without tripping recovery backoff."""

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    @asynccontextmanager
    async def scope() -> Session:
        yield Session()

    row = _desired_row()
    device = type("Device", (), {"appium_node": object()})()
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    monkeypatch.setattr(
        appium_reconciler,
        "_start_for_node",
        AsyncMock(side_effect=NodeAlreadyRunningError("already running for target on port 4724")),
    )
    record = AsyncMock()
    monkeypatch.setattr(appium_reconciler, "_record_start_failure", record)

    svc = ReconcilerService(
        publisher=Mock(), settings=FakeSettingsReader({}), pool=Mock(), circuit_breaker=Mock(), session_factory=Mock()
    )
    start = svc._make_start_agent(session_scope=scope)
    with pytest.raises(NodeAlreadyRunningError):
        await start(row=row, port=4723)

    record.assert_not_awaited()


async def test_converge_host_rows_downgrades_transient_stop_not_acknowledged(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop the agent won't acknowledge is a self-healing transient: the host
    convergence loop swallows it (raise_errors=False) and re-raises only when
    the caller asks (raise_errors=True, the converge_device_now path)."""
    # desired=stopped + an observed node → decide_convergence_action picks ``stop``.
    row = _desired_row(desired_state="stopped", connection_target="serial-stop", port=4723, pid=1)
    observed = [ObservedEntry(port=4723, pid=1, connection_target="serial-stop")]
    monkeypatch.setattr("app.appium_nodes.services.reconciler.stop_remote_node", AsyncMock(return_value=False))

    svc = ReconcilerService(
        publisher=Mock(), settings=FakeSettingsReader({}), pool=Mock(), circuit_breaker=Mock(), session_factory=Mock()
    )
    host_id = uuid.uuid4()

    # raise_errors=False → swallowed (no raise).
    await svc.converge_host_rows(
        None, [row], observed, host_id=host_id, host_ip="10.0.0.1", agent_port=5100, raise_errors=False
    )

    # raise_errors=True (converge_device_now path) → propagates the transient.
    with pytest.raises(NodeStopNotAcknowledgedError):
        await svc.converge_host_rows(
            None, [row], observed, host_id=host_id, host_ip="10.0.0.1", agent_port=5100, raise_errors=True
        )

    assert appium_reconciler._classify_start_failure(TimeoutError()) == "timeout"
    request = httpx.Request("POST", "http://agent/appium")
    response = httpx.Response(409, text="port is busy", request=request)
    assert appium_reconciler._classify_start_failure(
        httpx.HTTPStatusError("bad", request=request, response=response)
    ) == ("port_occupied")
    response = httpx.Response(500, text="already_running", request=request)
    assert appium_reconciler._classify_start_failure(
        httpx.HTTPStatusError("bad", request=request, response=response)
    ) == ("already_running")
    assert appium_reconciler._classify_start_failure(httpx.ConnectError("down")) == "http_error"
    assert appium_reconciler._classify_start_failure(RuntimeError("boom")) == "http_error"


async def test_start_agent_and_empty_helpers_remaining_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert appium_reconciler._session_scope(None) is appium_reconciler.async_session

    @asynccontextmanager
    async def _mock_session_factory() -> AsyncMock:
        yield AsyncMock()

    await appium_reconciler._touch_last_observed(
        [], settings=FakeSettingsReader({}), session_factory=_mock_session_factory
    )

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    row = _desired_row()

    @asynccontextmanager
    async def scope() -> Session:
        yield Session()

    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=None))
    svc2 = ReconcilerService(
        publisher=Mock(), settings=FakeSettingsReader({}), pool=Mock(), circuit_breaker=Mock(), session_factory=Mock()
    )
    start = svc2._make_start_agent(session_scope=scope)
    with pytest.raises(RuntimeError, match="no longer exists"):
        await start(row=row, port=4723)

    monkeypatch.setattr(
        appium_reconciler,
        "_load_device_for_reconciler",
        AsyncMock(return_value=type("Device", (), {"appium_node": None})()),
    )
    monkeypatch.setattr(appium_reconciler, "_record_start_failure", AsyncMock())
    with pytest.raises(RuntimeError, match="has no AppiumNode"):
        await start(row=row, port=4723)

    monkeypatch.setattr(appium_reconciler, "_lock_device_for_reconciler", AsyncMock(return_value=None))
    await appium_reconciler._reset_start_failure(row, session_scope=scope, settings=FakeSettingsReader({}))


async def test_record_and_reset_start_failure_state(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Start Failure Device",
        identity_value="reconciler-failure-001",
        operational_state=DeviceOperationalState.available,
    )
    row = _desired_row(device_id=device.id)

    @asynccontextmanager
    async def _scope() -> AsyncSession:
        yield db_session

    await appium_reconciler._record_start_failure(
        row,
        reason="timeout",
        session_scope=_scope,
        settings=FakeSettingsReader({"appium_reconciler.start_failure_threshold": 1, "appium.startup_timeout_sec": 5}),
    )
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 1
    assert device.lifecycle_policy_state["backoff_until"] is not None
    assert device.lifecycle_policy_state["last_failure_source"] == "appium_reconciler"
    assert device.lifecycle_policy_state["last_failure_reason"] == "timeout"

    await appium_reconciler._reset_start_failure(row, session_scope=_scope, settings=FakeSettingsReader({}))
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("recovery_backoff_attempts") in (None, 0)
    assert device.lifecycle_policy_state.get("last_failure_source") is None
    assert device.lifecycle_policy_state.get("last_failure_reason") is None

    monkeypatch.setattr(
        "app.appium_nodes.services.reconciler._lock_device_for_reconciler", AsyncMock(return_value=None)
    )
    await appium_reconciler._record_start_failure(
        _desired_row(device_id=uuid.uuid4()),
        reason="timeout",
        session_scope=_scope,
        settings=FakeSettingsReader({"appium_reconciler.start_failure_threshold": 1, "appium.startup_timeout_sec": 5}),
    )


async def test_reset_start_failure_noop_for_non_reconciler_source(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Non-Reconciler Failure Device",
        identity_value="non-reconciler-001",
        operational_state=DeviceOperationalState.available,
    )
    device.lifecycle_policy_state = {
        "last_failure_source": "connectivity",
        "last_failure_reason": "ping_timeout",
    }
    await db_session.commit()

    row = _desired_row(device_id=device.id)

    @asynccontextmanager
    async def _scope() -> AsyncSession:
        yield db_session

    await appium_reconciler._reset_start_failure(row, session_scope=_scope, settings=FakeSettingsReader({}))
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("last_failure_source") == "connectivity"
    assert device.lifecycle_policy_state.get("last_failure_reason") == "ping_timeout"


async def test_reset_start_failure_clears_orphaned_reason(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Orphaned Reason Device",
        identity_value="orphaned-reason-001",
        operational_state=DeviceOperationalState.available,
    )
    device.lifecycle_policy_state = {
        "last_failure_source": None,
        "last_failure_reason": "ghost_error",
    }
    await db_session.commit()

    row = _desired_row(device_id=device.id)

    @asynccontextmanager
    async def _scope() -> AsyncSession:
        yield db_session

    await appium_reconciler._reset_start_failure(row, session_scope=_scope, settings=FakeSettingsReader({}))
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("last_failure_source") is None
    assert device.lifecycle_policy_state.get("last_failure_reason") is None


async def test_confirm_running_skips_lock_when_no_failure_residue(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock must NOT be acquired for confirm_running when lifecycle_policy_state has no residue."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Clean Running Device",
        identity_value="confirm-running-clean-001",
        operational_state=DeviceOperationalState.available,
    )
    lock_mock = AsyncMock(return_value=device)
    monkeypatch.setattr("app.appium_nodes.services.reconciler._lock_device_for_reconciler", lock_mock)

    @asynccontextmanager
    async def _scope() -> AsyncSession:
        yield db_session

    reset_fn = appium_reconciler.ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=None,
        circuit_breaker=Mock(),
        session_factory=_scope,
    )._make_reset_start_failure(session_scope=_scope)

    # Row with no failure residue in lifecycle_policy_state
    row = _desired_row(device_id=device.id, lifecycle_policy_state={})
    from app.appium_nodes.services.reconciler_convergence import ConvergenceAction, _execute_action

    await _execute_action(
        host_id=db_host.id,
        row=row,
        action=ConvergenceAction(kind="confirm_running"),
        start_agent=AsyncMock(),
        stop_agent=AsyncMock(),
        write_observed=AsyncMock(),
        clear_token=AsyncMock(),
        reset_start_failure=reset_fn,
    )
    lock_mock.assert_not_awaited()


async def test_confirm_running_acquires_lock_when_failure_residue_present(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock MUST be acquired and state cleared when residue (recovery_backoff_attempts) is present."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Residue Running Device",
        identity_value="confirm-running-residue-001",
        operational_state=DeviceOperationalState.available,
    )
    device.lifecycle_policy_state = {
        "recovery_backoff_attempts": 2,
        "backoff_until": None,
        "last_failure_source": "appium_reconciler",
        "last_failure_reason": "timeout",
    }
    await db_session.commit()

    @asynccontextmanager
    async def _scope() -> AsyncSession:
        yield db_session

    reset_fn = appium_reconciler.ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=None,
        circuit_breaker=Mock(),
        session_factory=_scope,
    )._make_reset_start_failure(session_scope=_scope)

    # Row carries the same residue state lock-free
    row = _desired_row(
        device_id=device.id,
        lifecycle_policy_state={
            "recovery_backoff_attempts": 2,
            "backoff_until": None,
            "last_failure_source": "appium_reconciler",
            "last_failure_reason": "timeout",
        },
    )
    from app.appium_nodes.services.reconciler_convergence import ConvergenceAction, _execute_action

    await _execute_action(
        host_id=db_host.id,
        row=row,
        action=ConvergenceAction(kind="confirm_running"),
        start_agent=AsyncMock(),
        stop_agent=AsyncMock(),
        write_observed=AsyncMock(),
        clear_token=AsyncMock(),
        reset_start_failure=reset_fn,
    )
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("recovery_backoff_attempts") in (None, 0)
    assert device.lifecycle_policy_state.get("last_failure_source") is None


async def test_clear_transition_token_and_touch_noop(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def _mock_session_factory() -> AsyncMock:
        yield AsyncMock()

    await appium_reconciler._touch_last_observed(
        [], settings=FakeSettingsReader({}), session_factory=_mock_session_factory
    )
    with monkeypatch.context() as ctx:
        ctx.setattr("app.appium_nodes.services.reconciler._lock_device_for_reconciler", AsyncMock(return_value=None))
        await appium_reconciler._clear_transition_token(db_session, _desired_row(device_id=uuid.uuid4()))

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Clear Token Device",
        identity_value="reconciler-clear-001",
        operational_state=DeviceOperationalState.available,
    )
    token = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=30),
    )
    db_session.add(node)
    await db_session.commit()

    await appium_reconciler._clear_transition_token(
        db_session,
        _desired_row(device_id=device.id, node_id=node.id),
    )
    await db_session.refresh(node)
    assert node.transition_token is None
