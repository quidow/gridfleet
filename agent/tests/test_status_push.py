from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator  # noqa: TC003 - contextmanager signature is runtime-inspected
from typing import Any

import httpx2 as httpx
import pytest

from agent_app.config import agent_settings
from agent_app.host.capabilities import CapabilitiesCache
from agent_app.http_client import close as close_shared_http_client
from agent_app.lifespan import HttpStatusPushClient
from agent_app.pack.host_identity import HostIdentity
from agent_app.status_push import BootFenceRejected, StatusPushLoop

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
async def test_build_payload_includes_probe_sections() -> None:
    probe_sections = {
        "node_health": {"reported_at": "now", "nodes": []},
        "device_health": {"reported_at": "now", "devices": {}},
        "device_properties": {"reported_at": "now", "devices": {}},
    }
    loop = StatusPushLoop(
        client=RecordingClient(),
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(HOST_ID),
        pack_status=lambda: None,
        probe_results=lambda: probe_sections,
    )

    payload = await loop.build_payload()

    assert {"node_health", "device_health", "device_properties"} <= set(payload)
    assert payload["device_health"] == probe_sections["device_health"]


@pytest.mark.asyncio
async def test_build_payload_omits_probe_sections_when_unavailable() -> None:
    loop = StatusPushLoop(
        client=RecordingClient(),
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(HOST_ID),
        pack_status=lambda: None,
        probe_results=lambda: None,
    )

    payload = await loop.build_payload()

    assert not {"node_health", "device_health", "device_properties"} & set(payload)


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


class _ScriptedClient:
    """Replays a script of push outcomes: ``True`` rejects with the fence error."""

    def __init__(self, script: list[bool]) -> None:
        self.attempts = 0
        self._script = script

    async def post_status(self, payload: dict[str, Any]) -> None:
        rejects = self._script[self.attempts] if self.attempts < len(self._script) else False
        self.attempts += 1
        if rejects:
            raise BootFenceRejected("boot fence lost")


async def _wait_for_attempts(client: _ScriptedClient, count: int) -> None:
    while client.attempts < count:
        await asyncio.sleep(0.01)


async def _run_scripted_loop(script: list[bool], *, reregister_min_interval: float) -> int:
    """Run the loop until the script is exhausted; return the re-registration count."""
    client = _ScriptedClient(script)
    calls = 0

    def _on_fence() -> None:
        nonlocal calls
        calls += 1

    loop = StatusPushLoop(
        client=client,
        manager=_FakeManager(),
        capabilities_cache=await _capabilities_cache(),
        host_identity=_identity(HOST_ID),
        pack_status=lambda: None,
        push_interval=0.01,
        on_boot_fence_rejected=_on_fence,
        reregister_min_interval=reregister_min_interval,
    )
    task = asyncio.create_task(loop.run_forever())
    try:
        await asyncio.wait_for(_wait_for_attempts(client, len(script)), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return calls


@pytest.mark.asyncio
async def test_fence_rejection_reregisters_once_per_episode() -> None:
    """A burst of rejections is one episode: re-register once, not once per push."""
    assert await _run_scripted_loop([True, True, True], reregister_min_interval=3600.0) == 1


@pytest.mark.asyncio
async def test_refresh_interval_floors_the_next_reregistration() -> None:
    """A successful push ends the episode, but the floor still gates the next
    attempt — two agents disputing ownership must not ping-pong registrations."""
    assert await _run_scripted_loop([True, False, True], reregister_min_interval=3600.0) == 1


@pytest.mark.asyncio
async def test_a_later_episode_reregisters_once_the_floor_has_elapsed() -> None:
    assert await _run_scripted_loop([True, False, True], reregister_min_interval=0.0) == 2


@pytest.mark.asyncio
async def test_fence_rejection_without_a_callback_does_not_kill_the_loop() -> None:
    client = _ScriptedClient([True, True])
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
        await asyncio.wait_for(_wait_for_attempts(client, 2), timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _ConflictTransport(httpx.AsyncBaseTransport):
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json=self._body, request=request)


@contextlib.asynccontextmanager
async def _push_client_over(
    transport: httpx.AsyncBaseTransport, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[HttpStatusPushClient]:
    await close_shared_http_client()
    original_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    try:
        yield HttpStatusPushClient("http://manager.local")
    finally:
        await close_shared_http_client()


@pytest.mark.asyncio
async def test_push_client_raises_boot_fence_rejected_on_the_coded_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wire value, spelled out: importing the constant here would let a
    # rename travel silently from one side of the contract to the other.
    transport = _ConflictTransport(
        {"error": {"code": "BOOT_FENCE_SUPERSEDED", "message": "Stale or superseded boot_id", "request_id": None}}
    )
    async with _push_client_over(transport, monkeypatch) as client:
        with pytest.raises(BootFenceRejected):
            await client.post_status({"host_id": HOST_ID})


@pytest.mark.asyncio
async def test_push_client_leaves_an_uncoded_409_as_a_plain_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated conflict must not trigger re-enrolment."""
    transport = _ConflictTransport(
        {"error": {"code": "CONFLICT", "message": "something else entirely", "request_id": None}}
    )
    async with _push_client_over(transport, monkeypatch) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.post_status({"host_id": HOST_ID})


async def _wait_for_registrations(recorded: list[httpx.Request], count: int) -> None:
    while len(recorded) < count:
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_lifespan_recovers_the_fence_within_a_push_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fenced-out push re-registers through the real lifespan.

    The refresh interval is 300 s, so a second POST to /api/hosts/register
    inside this timeout can only come from the fence hook. That second
    registration is what rewrites the fence and puts the host back online.
    """
    from unittest.mock import AsyncMock, patch

    from agent_app.host.capabilities import CapabilitiesCache
    from agent_app.lifespan import lifespan
    from agent_app.main import app

    host_id = "00000000-0000-0000-0000-000000000042"
    registrations: list[httpx.Request] = []

    class _FenceTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/hosts/register":
                registrations.append(request)
                return httpx.Response(200, json={"id": host_id, "status": "online"}, request=request)
            if request.url.path == "/agent/hosts/status":
                return httpx.Response(
                    409,
                    json={"error": {"code": "BOOT_FENCE_SUPERSEDED", "message": "Stale or superseded boot_id"}},
                    request=request,
                )
            return httpx.Response(200, json={}, request=request)

    await close_shared_http_client()
    original_async_client = httpx.AsyncClient

    def client_factory(**kwargs: object) -> httpx.AsyncClient:
        return original_async_client(transport=_FenceTransport(), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    monkeypatch.setattr(agent_settings.core, "status_push_interval_sec", 0.05)
    monkeypatch.setattr(agent_settings.core, "host_id", host_id)

    try:
        with (
            patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
            patch.object(CapabilitiesCache, "run_refresh_loop", new_callable=AsyncMock),
            patch.object(CapabilitiesCache, "get_or_refresh", new_callable=AsyncMock, return_value={}),
            patch("agent_app.host.hardware_info.collect", return_value={}),
            patch("agent_app.status_push.get_host_telemetry", new_callable=AsyncMock, return_value={}),
            patch("agent_app.appium.appium_mgr.start_log_maintenance"),
            patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
        ):
            async with lifespan(app):
                await asyncio.wait_for(_wait_for_registrations(registrations, 2), timeout=5.0)
    finally:
        await close_shared_http_client()

    assert len(registrations) >= 2
