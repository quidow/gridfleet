"""Reconciler convergence integration tests with real DB rows and mocked agent calls."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.services.lifecycle_policy_state import state as lifecycle_policy_state
from app.services.node_service_types import RemoteStartResult
from app.services.settings_service import settings_service
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


@pytest.fixture(autouse=True)
def disable_reconciler_fencing_for_integration_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.appium_reconciler.assert_current_leader", AsyncMock())


class _SharedSessionContext:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def __aenter__(self) -> AsyncSession:
        return self._db

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _session_factory(db: AsyncSession) -> object:
    def _factory() -> _SharedSessionContext:
        return _SharedSessionContext(db)

    return _factory


async def test_reconciler_starts_agent_when_desired_running_and_no_observed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-start", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=0,
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    start_mock = AsyncMock(
        return_value=RemoteStartResult(
            port=4723,
            pid=12345,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
                    )
    )

    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=start_mock),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert node.observed_running
    assert node.port == 4723
    assert node.pid == 12345
    start_mock.assert_awaited_once()


async def test_reconciler_does_not_reuse_stale_running_db_row_when_agent_reports_absent(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-stale-running", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=111,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    start_mock = AsyncMock(
        return_value=RemoteStartResult(
            port=4723,
            pid=222,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
                    )
    )
    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=start_mock),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    start_mock.assert_awaited_once()
    await db_session.refresh(node)
    assert node.observed_running
    assert node.pid == 222


async def test_reconciler_stops_agent_when_desired_stopped_and_observed(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-stop", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=12345,
        desired_state=AppiumDesiredState.stopped,
        desired_port=None,
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    stop_mock = AsyncMock(return_value=True)
    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 12345,
                    "connection_target": device.connection_target,
                    "platform_id": device.platform_id,
                }
            ],
        }
    }
    with (
        patch.object(appium_reconciler, "agent_health", new=AsyncMock(return_value=payload)),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=AsyncMock()),
        patch.object(appium_reconciler, "stop_remote_node", new=stop_mock),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert not node.observed_running
    assert node.pid is None
    stop_mock.assert_awaited_once()


async def test_reconciler_stop_intent_clears_restart_transition_token(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-stop-clears-token", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=12345,
        desired_state=AppiumDesiredState.stopped,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=60),
        active_connection_target=device.connection_target,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 12345,
                    "connection_target": device.connection_target,
                    "platform_id": device.platform_id,
                }
            ],
        }
    }
    with (
        patch.object(appium_reconciler, "agent_health", new=AsyncMock(return_value=payload)),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=AsyncMock()),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock(return_value=True)),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert not node.observed_running
    assert node.transition_token is None
    assert node.transition_deadline is None


async def test_reconciler_restarts_agent_and_clears_transition_token(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-restart", verified=True)
    token = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=111,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=60),
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 111,
                    "connection_target": device.connection_target,
                    "platform_id": device.platform_id,
                }
            ],
        }
    }
    start_mock = AsyncMock(
        return_value=RemoteStartResult(
            port=4724,
            pid=222,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
                    )
    )
    stop_mock = AsyncMock(return_value=True)

    with (
        patch.object(appium_reconciler, "agent_health", new=AsyncMock(return_value=payload)),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=start_mock),
        patch.object(appium_reconciler, "stop_remote_node", new=stop_mock),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert node.observed_running
    assert node.port == 4724
    assert node.pid == 222
    assert node.transition_token is None
    assert node.transition_deadline is None
    stop_mock.assert_awaited_once()
    start_mock.assert_awaited_once()


async def test_reconciler_failed_start_sets_backoff_and_success_resets_it(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-backoff", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=settings_service.get("appium.port_range_start"),
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=settings_service.get("appium.port_range_start"),
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    monkeypatch.setitem(settings_service._cache, "appium_reconciler.start_failure_threshold", 1)
    failing_start = AsyncMock(side_effect=RuntimeError("agent start failed"))
    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=failing_start),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(device)
    failed_state = lifecycle_policy_state(device)
    assert failed_state["recovery_backoff_attempts"] == 1
    assert failed_state["backoff_until"] is not None

    expired_state = dict(device.lifecycle_policy_state or {})
    expired_state["backoff_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    device.lifecycle_policy_state = expired_state
    node.desired_port = settings_service.get("appium.port_range_start") + 1
    db_session.add_all([device, node])
    await db_session.commit()
    success_start = AsyncMock(
        return_value=RemoteStartResult(
            port=node.desired_port,
            pid=333,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
                    )
    )
    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=success_start),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(device)
    recovered_state = lifecycle_policy_state(device)
    assert recovered_state["recovery_backoff_attempts"] == 0
    assert recovered_state["backoff_until"] is None


async def test_reconciler_stop_failure_preserves_restart_token(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-stop-fail", verified=True)
    token = uuid.uuid4()
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=111,
        desired_state=AppiumDesiredState.running,
        desired_port=4724,
        transition_token=token,
        transition_deadline=datetime.now(UTC) + timedelta(seconds=60),
        active_connection_target=device.identity_value,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    payload = {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 111,
                    "connection_target": device.connection_target,
                    "platform_id": device.platform_id,
                }
            ],
        }
    }
    with (
        patch.object(appium_reconciler, "agent_health", new=AsyncMock(return_value=payload)),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=AsyncMock()),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock(return_value=False)),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert node.transition_token == token
    assert node.transition_deadline is not None
    assert node.observed_running


async def test_reconciler_touches_backed_off_rows_when_host_responds(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-backoff-touch", verified=True)
    device.lifecycle_policy_state = {"backoff_until": (datetime.now(UTC) + timedelta(minutes=5)).isoformat()}
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        last_observed_at=None,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    start_mock = AsyncMock()
    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=start_mock),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert node.last_observed_at is not None
    start_mock.assert_not_awaited()


async def test_reconciler_rejects_zero_port_start_result(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-zero", verified=True)
    node = AppiumNode(
        device_id=device.id,
        port=0,
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
    )
    db_session.add(node)
    await db_session.commit()

    from app.services import appium_reconciler

    start_mock = AsyncMock(
        return_value=RemoteStartResult(port=0, pid=444, active_connection_target=device.identity_value)
    )
    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch.object(appium_reconciler, "_start_for_node", new=start_mock),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(node)
    assert not node.observed_running
    assert node.pid is None


async def test_fetch_backoff_until_coerces_naive_datetimes_to_utc(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="conv-naive-backoff", verified=True)
    device.lifecycle_policy_state = {"backoff_until": "2026-05-10T12:00:00"}
    await db_session.commit()

    from app.services import appium_reconciler

    backoff = await appium_reconciler._fetch_backoff_until(db_session)

    assert backoff[device.id].tzinfo == UTC


async def test_reconciler_allocates_distinct_ports_for_two_same_host_starts(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    start_port = settings_service.get("appium.port_range_start")
    first = await create_device(db_session, host_id=db_host.id, name="conv-alloc-a", verified=True)
    second = await create_device(db_session, host_id=db_host.id, name="conv-alloc-b", verified=True)
    first_node = AppiumNode(
        device_id=first.id,
        port=start_port,
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=start_port,
    )
    second_node = AppiumNode(
        device_id=second.id,
        port=start_port + 1,
        grid_url="http://hub:4444",
        pid=None,
        active_connection_target=None,
        desired_state=AppiumDesiredState.running,
        desired_port=start_port,
    )
    db_session.add_all([first_node, second_node])
    await db_session.commit()

    from app.services import appium_reconciler

    async def start_remote(*args: object, **kwargs: object) -> RemoteStartResult:
        device = args[1]
        assert hasattr(device, "identity_value")
        port = kwargs["port"]
        assert isinstance(port, int)
        return RemoteStartResult(
            port=port,
            pid=1000 + port,
            active_connection_target=device.identity_value,
            agent_base="http://agent",
        )

    with (
        patch.object(
            appium_reconciler, "agent_health", new=AsyncMock(return_value={"appium_processes": {"running_nodes": []}})
        ),
        patch.object(appium_reconciler, "async_session", new=_session_factory(db_session)),
        patch(
            "app.services.appium_reconciler_agent.start_remote_node", new=AsyncMock(side_effect=start_remote)
        ),
        patch.object(appium_reconciler, "stop_remote_node", new=AsyncMock()),
    ):
        await appium_reconciler.run_one_cycle_for_test()

    await db_session.refresh(first_node)
    await db_session.refresh(second_node)
    assert first_node.observed_running
    assert second_node.observed_running
    assert first_node.port != second_node.port
    assert first_node.port >= start_port
    assert second_node.port >= start_port
