"""Regression guard: backend→agent call sites must forward the auth-carrying pool.

Agent BasicAuth is carried only by ``AgentHttpPool.auth`` (see
``app.agent_comm.operations._send_request``). A call site that omits ``pool=``
sends no Authorization header, so the agent rejects it with HTTP 401 when
``GRIDFLEET_AUTH_ENABLED=true``. These tests assert each previously pool-less
call site now forwards the injected pool to the agent operation.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock
from uuid import uuid4

import httpx

from app.agent_comm.http_pool import AgentHttpPool
from app.devices.models import ConnectionType, DeviceType
from app.devices.services import connectivity as device_connectivity
from app.packs.services import discovery as pack_discovery
from app.packs.services.discovery import PackDiscoveryService
from app.verification.services import execution as verification_execution
from app.verification.services.execution import VerificationExecutionService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest


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


async def test_connectivity_get_lifecycle_state_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    action = AsyncMock(return_value={"state": "ready"})
    monkeypatch.setattr(device_connectivity, "pack_device_lifecycle_action", action)
    monkeypatch.setattr(
        device_connectivity,
        "resolve_pack_platform",
        AsyncMock(return_value=SimpleNamespace(lifecycle_actions={"state": {}})),
    )
    monkeypatch.setattr(device_connectivity, "platform_has_lifecycle_action", Mock(return_value=True))

    await device_connectivity._get_lifecycle_state(
        object(), _conn_device(), settings=FakeSettingsReader(), circuit_breaker=Mock(), pool=pool
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
