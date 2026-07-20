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

import httpx2 as httpx

from app.agent_comm.http_pool import AgentHttpPool
from app.devices.models import ConnectionType, DeviceType
from app.packs.services import discovery as pack_discovery
from app.packs.services.discovery import PackDiscoveryService
from app.verification.services import execution as verification_execution
from app.verification.services.execution import AgentCallContext, VerificationExecutionService
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import pytest


def _auth_pool() -> AgentHttpPool:
    return AgentHttpPool(agent_auth=httpx.BasicAuth("ops", "secret"))


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
        appium_node=None,
    )

    await VerificationExecutionService(
        review=build_review_service(),
        publisher=event_bus,
        agent=AgentCallContext(settings=settings, circuit_breaker=Mock(), pool=pool),
        crud=AsyncMock(),
        viability=Mock(),
        capability=Mock(),
        reconciler=AsyncMock(),
        node_manager=AsyncMock(),
    ).run_device_health({"stages": []}, device, http_client_factory=MagicMock())

    assert fetch.await_args.kwargs["pool"] is pool


async def test_discovery_forwards_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = _auth_pool()
    fetcher = AsyncMock(return_value={"candidates": []})
    monkeypatch.setattr(pack_discovery.platform_label_service, "load_platform_label_map", AsyncMock(return_value={}))

    service = PackDiscoveryService(
        agent_get_pack_devices=fetcher,
        circuit_breaker=Mock(),
        serializer=Mock(),
        identity_guard=Mock(),
        pool=pool,
    )
    host = SimpleNamespace(ip="10.0.0.10", agent_port=5100)
    await service.list_intake_candidates(Mock(), host)

    assert fetcher.await_args.kwargs["pool"] is pool
