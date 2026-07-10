import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from sqlalchemy.exc import NoResultFound

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_convergence import DesiredRow
from app.devices.models import DeviceOperationalState
from app.hosts.models import Host, HostStatus
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device

if TYPE_CHECKING:
    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession


def _desired_row(**overrides: object) -> DesiredRow:
    values: dict[str, object] = {
        "device_id": uuid.uuid4(),
        "host_id": uuid.uuid4(),
        "node_id": uuid.uuid4(),
        "connection_target": "serial-1",
        "desired_state": "running",
        "desired_port": 4723,
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
    restart_requested_at = datetime(2026, 7, 9, 15, 0, tzinfo=UTC)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        restart_requested_at=restart_requested_at,
        pid=123,
        active_connection_target="reconciler-target",
        stop_pending=True,
    )
    db_session.add(node)
    await db_session.commit()

    desired = await appium_reconciler.fetch_desired_rows(db_session, offline_after_sec=45)
    desired_one = await appium_reconciler._fetch_desired_row(db_session, device.id)
    missing = await appium_reconciler._fetch_desired_row(db_session, uuid.uuid4())
    backoff = await appium_reconciler.fetch_backoff_until(db_session)

    assert desired[0].started_at == node.started_at
    assert desired_one is not None
    assert desired_one.stop_pending is True
    assert missing is None
    assert device.id in backoff

    device.lifecycle_policy_state = {"backoff_until": "not-a-date"}
    await db_session.commit()
    assert await appium_reconciler.fetch_backoff_until(db_session) == {}


async def test_fetch_desired_rows_includes_recovered_host_before_ledger_flips(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    # Ledger still says offline (the sweep edge hasn't run yet) but the host is
    # pushing again: its desired rows must converge in the same tick.
    db_host.status = HostStatus.offline
    db_host.last_heartbeat = datetime.now(UTC)
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="Recovered Device",
        identity_value="recovered-001",
        connection_target="recovered-target",
        operational_state=DeviceOperationalState.available,
    )
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=4723,
            desired_state=AppiumDesiredState.running,
            desired_port=4723,
        )
    )
    await db_session.commit()

    rows = await appium_reconciler.fetch_desired_rows(db_session, offline_after_sec=45)
    assert any(r.host_id == db_host.id for r in rows)


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
        settings=FakeSettingsReader({}),
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
        settings=FakeSettingsReader({}),
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
        write_observed=AsyncMock(),
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
        write_observed=AsyncMock(),
        reset_start_failure=reset_fn,
    )
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("recovery_backoff_attempts") in (None, 0)
    assert device.lifecycle_policy_state.get("last_failure_source") is None


async def test_touch_last_observed_noop() -> None:
    @asynccontextmanager
    async def _mock_session_factory() -> AsyncMock:
        yield AsyncMock()

    await appium_reconciler._touch_last_observed(
        [], settings=FakeSettingsReader({}), session_factory=_mock_session_factory
    )
