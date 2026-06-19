"""Regression guard: backend→agent call sites must forward the auth-carrying pool.

Agent BasicAuth is carried only by ``AgentHttpPool.auth`` (see
``app.agent_comm.operations._send_request``). A call site that omits ``pool=``
sends no Authorization header, so the agent rejects it with HTTP 401 when
``GRIDFLEET_AUTH_ENABLED=true``. These tests assert each previously pool-less
call site now forwards the injected pool to the agent operation.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import httpx2 as httpx
import pytest

from app.agent_comm.http_pool import AgentHttpPool
from app.appium_nodes.exceptions import NodeAlreadyRunningError
from app.appium_nodes.services import reconciler as appium_reconciler
from app.appium_nodes.services import reconciler_agent as node_agent
from app.appium_nodes.services.reconciler import ReconcilerService
from app.appium_nodes.services.reconciler_agent import RemoteStartResult, start_remote_node, stop_remote_node
from app.devices.models import ConnectionType, DeviceType
from app.devices.services import connectivity as device_connectivity
from app.packs.services import discovery as pack_discovery
from app.packs.services.discovery import PackDiscoveryService
from app.verification.services import execution as verification_execution
from app.verification.services.execution import VerificationExecutionService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def _auth_pool() -> AgentHttpPool:
    return AgentHttpPool(agent_auth=httpx.BasicAuth("ops", "secret"))


def _conn_device() -> SimpleNamespace:
    host = SimpleNamespace(ip="10.0.0.10", agent_port=5100)
    return SimpleNamespace(
        id=uuid4(),
        host=host,
        host_id=uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        connection_target="demo",
        identity_value="demo",
    )


async def test_connectivity_get_device_health_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(device_connectivity, "fetch_pack_device_health", fetch)

    await device_connectivity._get_device_health(
        _conn_device(), settings=FakeSettingsReader(), circuit_breaker=Mock(), pool=pool
    )

    assert fetch.await_args.kwargs["pool"] is pool


async def test_connectivity_get_agent_devices_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    fetch = AsyncMock(return_value={"candidates": []})
    monkeypatch.setattr(device_connectivity, "get_pack_devices", fetch)

    host = SimpleNamespace(ip="10.0.0.10", agent_port=5100)
    await device_connectivity._get_agent_devices(host, settings=FakeSettingsReader(), circuit_breaker=Mock(), pool=pool)

    assert fetch.await_args.kwargs["pool"] is pool


async def test_connectivity_fetch_lifecycle_state_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    action = AsyncMock(return_value={"state": "ready"})
    monkeypatch.setattr(device_connectivity, "pack_device_lifecycle_action", action)

    await device_connectivity._fetch_lifecycle_state(
        _conn_device(), settings=FakeSettingsReader(), circuit_breaker=Mock(), pool=pool
    )

    assert action.await_args.kwargs["pool"] is pool


async def test_verification_execution_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    monkeypatch.setattr(verification_execution, "set_stage", AsyncMock())
    fetch = AsyncMock(return_value={"healthy": True})
    monkeypatch.setattr(verification_execution, "fetch_pack_device_health", fetch)
    settings = FakeSettingsReader({})
    device = SimpleNamespace(
        id=uuid4(),
        host=SimpleNamespace(ip="10.0.0.1", agent_port=5100),
        host_id=uuid4(),
        pack_id="pack",
        platform_id="platform",
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
        ip_address=None,
        connection_target="target",
        identity_value="target",
        tags={},
        appium_node=None,
    )

    await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        settings=settings,
        circuit_breaker=Mock(),
        crud=AsyncMock(),
        viability=Mock(),
        capability=Mock(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
        pool=pool,
    ).run_device_health({"stages": []}, device, http_client_factory=MagicMock())

    assert fetch.await_args.kwargs["pool"] is pool


async def test_discovery_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    fetcher = AsyncMock(return_value={"candidates": []})
    monkeypatch.setattr(pack_discovery.platform_label_service, "load_platform_label_map", AsyncMock(return_value={}))

    service = PackDiscoveryService(
        agent_get_pack_devices=fetcher,
        agent_get_pack_device_properties=AsyncMock(return_value=None),
        settings=FakeSettingsReader(),
        circuit_breaker=Mock(),
        serializer=Mock(),
        identity_guard=Mock(),
        pool=pool,
    )
    host = SimpleNamespace(ip="10.0.0.10", agent_port=5100)
    await service.list_intake_candidates(Mock(), host)

    assert fetcher.await_args.kwargs["pool"] is pool


def _reconciler_service(pool: AgentHttpPool) -> ReconcilerService:
    return ReconcilerService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        pool=pool,
        circuit_breaker=Mock(),
        session_factory=Mock(),
    )


class _NullSession:
    async def __aenter__(self) -> _NullSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


async def test_reconciler_start_agent_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconciler convergence start path must forward the auth pool to the
    agent; otherwise node starts hit the agent with no Authorization header."""
    pool = _auth_pool()

    @asynccontextmanager
    async def scope() -> AsyncIterator[_NullSession]:
        yield _NullSession()

    row = SimpleNamespace(device_id=uuid4())
    device = SimpleNamespace(appium_node=object())
    monkeypatch.setattr(appium_reconciler, "_load_device_for_reconciler", AsyncMock(return_value=device))
    start_mock = AsyncMock(side_effect=NodeAlreadyRunningError("already running for target"))
    monkeypatch.setattr(appium_reconciler, "_start_for_node", start_mock)

    start = _reconciler_service(pool)._make_start_agent(require_leader=False, session_scope=scope)
    with pytest.raises(NodeAlreadyRunningError):
        await start(row=row, port=4723)

    assert start_mock.await_args.kwargs["pool"] is pool


async def test_reconciler_stop_agent_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconciler convergence stop path must forward the auth pool too."""
    pool = _auth_pool()
    stop_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(appium_reconciler, "stop_remote_node", stop_mock)

    stop = _reconciler_service(pool)._make_stop_agent("10.0.0.1", 5100)
    await stop(port=4723)

    assert stop_mock.await_args.kwargs["pool"] is pool


async def test_start_for_node_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    device = SimpleNamespace(
        id=uuid4(),
        host_id=uuid4(),
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
    )
    node = SimpleNamespace(id=uuid4())

    class _SessionFactory:
        def __call__(self) -> _SessionFactory:
            return self

        async def __aenter__(self) -> AsyncMock:
            return AsyncMock()

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(node_agent, "_short_session_factory", lambda _db: _SessionFactory())
    monkeypatch.setattr(node_agent, "resolve_pack_platform", AsyncMock(side_effect=LookupError()))
    monkeypatch.setattr(node_agent.appium_node_resource_service, "get_capabilities", AsyncMock(return_value={}))
    monkeypatch.setattr(node_agent, "agent_url", AsyncMock(return_value="http://agent"))
    monkeypatch.setattr(node_agent, "candidate_ports", AsyncMock(return_value=[4723]))
    monkeypatch.setattr(node_agent, "reserve_appium_port", AsyncMock())
    start_mock = AsyncMock(
        return_value=RemoteStartResult(port=4723, pid=1, active_connection_target="dev", agent_base="http://agent")
    )
    monkeypatch.setattr(node_agent, "start_remote_node", start_mock)

    await node_agent._start_for_node(
        AsyncMock(), device, node=node, settings=FakeSettingsReader({}), circuit_breaker=Mock(), pool=pool
    )

    assert start_mock.await_args.kwargs["pool"] is pool


async def test_start_remote_node_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    device = SimpleNamespace(id=uuid4(), pack_id="p", platform_id="pl")
    host = SimpleNamespace(ip="10.0.0.10", agent_port=5100, tool_env={})
    monkeypatch.setattr(node_agent, "assert_runnable", AsyncMock())
    monkeypatch.setattr(node_agent, "require_management_host", Mock(return_value=host))
    monkeypatch.setattr(node_agent, "resolve_pack_for_device", Mock(return_value=("p", "pl")))
    monkeypatch.setattr(node_agent, "render_stereotype", AsyncMock(return_value={}))
    monkeypatch.setattr(node_agent, "build_device_context", Mock(return_value={}))
    monkeypatch.setattr(
        node_agent, "resolve_pack_platform", AsyncMock(return_value=SimpleNamespace(appium_platform_name="Android"))
    )
    monkeypatch.setattr(node_agent, "_build_session_aligned_start_caps", AsyncMock(return_value={}))
    monkeypatch.setattr(node_agent, "build_agent_start_payload", Mock(return_value={}))
    monkeypatch.setattr(node_agent, "_merge_appium_default_pack_caps", AsyncMock())
    monkeypatch.setattr(node_agent, "build_pack_start_payload", AsyncMock(return_value=None))
    resp = MagicMock()
    resp.raise_for_status = Mock()
    start_mock = AsyncMock(return_value=resp)
    monkeypatch.setattr(node_agent, "appium_start", start_mock)
    monkeypatch.setattr(node_agent, "response_json_dict", Mock(return_value={"pid": 1, "connection_target": "dev"}))

    await start_remote_node(
        AsyncMock(),
        device,
        port=4723,
        allocated_caps=None,
        agent_base="http://10.0.0.10:5100",
        http_client_factory=AsyncMock(),
        settings=FakeSettingsReader({"appium.startup_timeout_sec": 30}),
        circuit_breaker=Mock(),
        pool=pool,
    )

    assert start_mock.await_args.kwargs["pool"] is pool


async def test_stop_remote_node_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    resp = MagicMock()
    resp.raise_for_status = Mock()
    stop_mock = AsyncMock(return_value=resp)
    monkeypatch.setattr(node_agent, "appium_stop", stop_mock)

    result = await stop_remote_node(
        port=4723,
        agent_base="http://10.0.0.10:5100",
        host="10.0.0.10",
        agent_port=5100,
        http_client_factory=AsyncMock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        pool=pool,
    )

    assert result is True
    assert stop_mock.await_args.kwargs["pool"] is pool
