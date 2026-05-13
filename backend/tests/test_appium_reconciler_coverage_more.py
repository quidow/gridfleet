import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device import DeviceOperationalState
from app.models.host import Host, HostStatus
from app.services import appium_reconciler
from app.services.appium_reconciler_convergence import DesiredRow
from tests.helpers import create_device


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


async def test_reconcile_host_and_loop_tick_skip_invalid_payloads_and_hosts() -> None:
    host_id = uuid.uuid4()
    stopped = await appium_reconciler.reconcile_host_orphans(
        host_id=host_id,
        host_ip="10.0.0.1",
        agent_port=5100,
        db_running_rows=[],
        fetch_health=AsyncMock(return_value={"appium_processes": []}),
        appium_stop=AsyncMock(),
    )
    assert stopped == []

    reconcile_host = AsyncMock(side_effect=[RuntimeError("host failed"), []])
    total = await appium_reconciler.appium_reconciler_loop_tick(
        list_online_hosts=AsyncMock(
            return_value=[
                {"id": "bad-id", "ip": "10.0.0.1", "agent_port": 5100},
                {"id": uuid.uuid4(), "ip": "10.0.0.2", "agent_port": "bad-port"},
                {"id": uuid.uuid4(), "ip": "10.0.0.3", "agent_port": 5100},
                {"id": uuid.uuid4(), "ip": "10.0.0.4", "agent_port": 5100},
            ]
        ),
        list_db_running_rows=AsyncMock(return_value=[]),
        reconcile_host=reconcile_host,
    )

    assert total == 0
    assert reconcile_host.await_count == 2


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
        grid_url="http://grid:4444",
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

    hosts = await appium_reconciler._fetch_online_hosts(db_session)
    rows = await appium_reconciler._fetch_node_rows(db_session)
    desired = await appium_reconciler._fetch_desired_rows(db_session)
    desired_one = await appium_reconciler._fetch_desired_row(db_session, device.id)
    missing = await appium_reconciler._fetch_desired_row(db_session, uuid.uuid4())
    backoff = await appium_reconciler._fetch_backoff_until(db_session)

    assert hosts == [{"id": db_host.id, "ip": db_host.ip, "agent_port": db_host.agent_port}]
    assert rows[0]["node_desired_state"] == "running"
    assert desired[0].transition_token == token
    assert desired_one is not None
    assert desired_one.stop_pending is True
    assert missing is None
    assert device.id in backoff

    device.lifecycle_policy_state = {"backoff_until": "not-a-date"}
    await db_session.commit()
    assert await appium_reconciler._fetch_backoff_until(db_session) == {}


async def test_drive_convergence_filters_hosts_and_uses_cached_health(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr("app.services.appium_reconciler._touch_last_observed", touch)
    monkeypatch.setattr("app.services.appium_reconciler.converge_host_rows", converge)
    monkeypatch.setattr("app.services.appium_reconciler.settings_service.get", lambda _key: 2)

    await appium_reconciler._drive_convergence(
        [
            {"id": "bad", "ip": "10.0.0.1", "agent_port": 5100},
            {"id": uuid.uuid4(), "ip": "10.0.0.2", "agent_port": 5100},
            {"id": host_id, "ip": "10.0.0.3", "agent_port": 5100},
        ],
        [active, backed_off],
        {backed_off.device_id: datetime.now(UTC) + timedelta(minutes=10)},
        health_by_host={host_id: observed_payload},
        require_leader=False,
    )

    touch.assert_awaited_once()
    converge.assert_awaited_once()
    assert converge.await_args.kwargs["rows"] == [active]


async def test_stop_agent_factory_and_start_failure_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _desired_row()
    stop_agent = appium_reconciler._make_stop_agent("10.0.0.1", 5100)
    assert await stop_agent(row=row, port=None) is None
    assert await stop_agent(row=row, port=0) is None

    monkeypatch.setattr("app.services.appium_reconciler.stop_remote_node", AsyncMock(return_value=False))
    with pytest.raises(RuntimeError, match="did not acknowledge"):
        await stop_agent(row=row, port=4723)

    monkeypatch.setattr(
        "app.services.appium_reconciler.stop_remote_node", AsyncMock(side_effect=httpx.ConnectError("down"))
    )
    with pytest.raises(httpx.ConnectError):
        await stop_agent(row=row, port=4723)

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

    monkeypatch.setattr(
        "app.services.appium_reconciler.settings_service.get",
        lambda key: {"appium_reconciler.start_failure_threshold": 1, "appium.startup_timeout_sec": 5}[key],
    )

    await appium_reconciler._record_start_failure(row, reason="timeout", require_leader=False, session_scope=_scope)
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state["recovery_backoff_attempts"] == 1
    assert device.lifecycle_policy_state["backoff_until"] is not None

    await appium_reconciler._reset_start_failure(row, require_leader=False, session_scope=_scope)
    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("recovery_backoff_attempts") in (None, 0)

    monkeypatch.setattr("app.services.appium_reconciler._lock_device_for_reconciler", AsyncMock(return_value=None))
    await appium_reconciler._record_start_failure(
        _desired_row(device_id=uuid.uuid4()),
        reason="timeout",
        require_leader=False,
        session_scope=_scope,
    )


async def test_clear_transition_token_and_touch_noop(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await appium_reconciler._touch_last_observed([])
    with monkeypatch.context() as ctx:
        ctx.setattr("app.services.appium_reconciler._lock_device_for_reconciler", AsyncMock(return_value=None))
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
        grid_url="http://grid:4444",
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
