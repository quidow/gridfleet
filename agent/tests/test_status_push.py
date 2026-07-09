from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import httpx2 as httpx
import pytest

from agent_app.config import agent_settings
from agent_app.host.capabilities import CapabilitiesCache
from agent_app.http_client import close as close_shared_http_client
from agent_app.lifespan import HttpStatusPushClient
from agent_app.pack.host_identity import HostIdentity
from agent_app.status_push import StatusPushLoop

HOST_ID = "00000000-0000-0000-0000-000000000001"


class _FakeManager:
    async def process_snapshot(self) -> dict[str, Any]:
        return {"running_nodes": [], "recent_restart_events": [], "start_failures": []}


class RecordingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post_status(self, payload: dict[str, Any]) -> None:
        self.posted.append(payload)


class _RaisingClient:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self._raise_next = True

    async def post_status(self, payload: dict[str, Any]) -> None:
        if self._raise_next:
            self._raise_next = False
            raise RuntimeError("post boom")
        self.posted.append(payload)


def _identity(host_id: str | None) -> HostIdentity:
    hi = HostIdentity()
    if host_id is not None:
        hi.set(host_id)
    return hi


async def _capabilities_cache() -> CapabilitiesCache:
    cache = CapabilitiesCache(adapter_registry=None)
    await cache.refresh()
    return cache


@pytest.mark.asyncio
async def test_build_payload_shape() -> None:
    loop = StatusPushLoop(
        client=RecordingClient(),
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(HOST_ID),
        pack_status=lambda: None,
    )

    payload = await loop.build_payload()

    assert payload["host_id"] == HOST_ID
    assert payload["agent_version"]
    assert "running_nodes" in payload["appium_processes"]
    assert payload["packs"] is None  # no pack reconcile yet
    assert {"recorded_at", "cpu_percent"} <= set(payload["host_telemetry"])


@pytest.mark.asyncio
async def test_wake_pushes_immediately() -> None:
    client = RecordingClient()
    loop = StatusPushLoop(
        client=client,
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(HOST_ID),
        pack_status=lambda: None,
        push_interval=3600.0,
    )
    task = asyncio.create_task(loop.run_forever())
    try:
        await asyncio.wait_for(_wait_for_count(client, 1), timeout=1.0)
        loop.wake()
        await asyncio.wait_for(_wait_for_count(client, 2), timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _wait_for_count(client: RecordingClient | _RaisingClient, count: int) -> None:
    while len(client.posted) < count:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_push_failure_does_not_kill_loop() -> None:
    client = _RaisingClient()
    loop = StatusPushLoop(
        client=client,
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(HOST_ID),
        pack_status=lambda: None,
        push_interval=0.01,
    )
    task = asyncio.create_task(loop.run_forever())
    try:
        await asyncio.wait_for(_wait_for_count(client, 1), timeout=1.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_payload_before_identity_raises() -> None:
    loop = StatusPushLoop(
        client=RecordingClient(),
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(None),
        pack_status=lambda: None,
    )

    with pytest.raises(RuntimeError):
        await loop.build_payload()


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(204, request=request)


@pytest.mark.asyncio
async def test_status_push_client_sends_manager_basic_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    await close_shared_http_client()
    monkeypatch.setattr(agent_settings.manager, "manager_auth_username", "machine")
    monkeypatch.setattr(agent_settings.manager, "manager_auth_password", "machine-secret")
    transport = RecordingTransport()
    original_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)

    try:
        client = HttpStatusPushClient("http://manager.local")
        await client.post_status({"host_id": HOST_ID})
    finally:
        await close_shared_http_client()

    assert len(transport.requests) == 1
    assert transport.requests[0].headers["authorization"].startswith("Basic ")
    assert transport.requests[0].url.path == "/agent/hosts/status"
