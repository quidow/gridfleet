import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.agent_client import request as agent_request
from app.config import Settings, settings
from app.database import build_engine
from app.errors import AgentUnreachableError, CircuitOpenError
from app.main import app
from app.middleware import RequestContextMiddleware
from app.services.agent_circuit_breaker import agent_circuit_breaker
from app.shutdown import shutdown_coordinator
from tests.helpers import create_host

HOST_PAYLOAD = {
    "hostname": "phase65-host",
    "ip": "10.0.0.31",
    "os_type": "linux",
    "agent_port": 5100,
}


async def test_agent_circuit_breaker_opens_then_recovers() -> None:
    current_time = 100.0

    def fake_monotonic() -> float:
        return current_time

    publish_mock = AsyncMock()

    with (
        patch("app.services.agent_circuit_breaker.monotonic", side_effect=fake_monotonic),
        patch("app.services.agent_circuit_breaker.event_bus.publish", new=publish_mock),
    ):
        for _ in range(agent_circuit_breaker.failure_threshold):
            await agent_circuit_breaker.record_failure("10.0.0.31", error="boom")

        snapshot = agent_circuit_breaker.snapshot("10.0.0.31")
        assert snapshot["status"] == "open"

        retry_after = await agent_circuit_breaker.before_request("10.0.0.31")
        assert retry_after is not None

        current_time += agent_circuit_breaker.cooldown_seconds
        assert await agent_circuit_breaker.before_request("10.0.0.31") is None
        assert await agent_circuit_breaker.before_request("10.0.0.31") == 0.0

        await agent_circuit_breaker.record_success("10.0.0.31")
        assert agent_circuit_breaker.snapshot("10.0.0.31")["status"] == "closed"

    opened_event = publish_mock.await_args_list[0].args[0]
    closed_event = publish_mock.await_args_list[-1].args[0]
    assert opened_event == "host.circuit_breaker.opened"
    assert closed_event == "host.circuit_breaker.closed"


async def test_agent_request_short_circuits_when_circuit_is_open() -> None:
    current_time = 200.0

    def fake_monotonic() -> float:
        return current_time

    with patch("app.services.agent_circuit_breaker.monotonic", side_effect=fake_monotonic):
        for _ in range(agent_circuit_breaker.failure_threshold):
            await agent_circuit_breaker.record_failure("10.0.0.32", error="boom")

        client = AsyncMock()
        with pytest.raises(CircuitOpenError):
            await agent_request(
                "GET",
                "http://10.0.0.32:5100/agent/health",
                endpoint="agent_health",
                host="10.0.0.32",
                client=client,
                timeout=5,
            )

    client.get.assert_not_awaited()


async def test_shutdown_coordinator_waits_for_active_requests() -> None:
    shutdown_coordinator.request_started()
    await shutdown_coordinator.begin_shutdown()

    waiter = asyncio.create_task(shutdown_coordinator.wait_for_drain(0.2))
    await asyncio.sleep(0)
    assert not waiter.done()

    shutdown_coordinator.request_finished()
    assert await waiter is True


async def test_request_timeout_middleware_returns_structured_error() -> None:
    async def slow_app(scope: Scope, receive: Receive, send: Send) -> None:
        await asyncio.sleep(0.02)
        response = JSONResponse({"ok": True})
        await response(scope, receive, send)

    middleware = RequestContextMiddleware(slow_app)
    middleware._request_timeout_sec = 0.001

    async with AsyncClient(transport=ASGITransport(app=middleware), base_url="http://test") as client:
        response = await client.get("/api/slow")

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "REQUEST_TIMEOUT"
    assert response.json()["error"]["message"] == "The request exceeded the maximum execution time"


async def test_shutdown_rejects_new_non_health_requests(client: AsyncClient) -> None:
    await shutdown_coordinator.begin_shutdown()

    health_response = await client.get("/health/live")
    hosts_response = await client.get("/api/hosts")

    assert health_response.status_code == 200
    assert hosts_response.status_code == 503
    assert hosts_response.json()["error"]["code"] == "SHUTTING_DOWN"


async def test_error_envelope_for_common_http_errors(client: AsyncClient) -> None:
    not_found = await client.get("/api/hosts/00000000-0000-0000-0000-000000000000")
    assert not_found.status_code == 404
    assert not_found.json()["error"]["code"] == "NOT_FOUND"
    assert not_found.json()["error"]["request_id"]

    await client.post("/api/hosts", json=HOST_PAYLOAD)
    conflict = await client.post("/api/hosts", json=HOST_PAYLOAD)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"

    validation = await client.get("/api/availability")
    assert validation.status_code == 422
    assert validation.json()["error"]["code"] == "VALIDATION_ERROR"
    assert isinstance(validation.json()["error"]["details"], list)


async def test_error_envelope_for_unhandled_exception(client: AsyncClient) -> None:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as uncaught_client:
        with patch("app.routers.hosts.host_service.list_hosts", new=AsyncMock(side_effect=RuntimeError("boom"))):
            response = await uncaught_client.get("/api/hosts")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert response.json()["error"]["request_id"]


async def test_error_envelope_for_agent_unreachable(client: AsyncClient) -> None:
    host = await create_host(client, **HOST_PAYLOAD)

    with patch(
        "app.routers.hosts.pack_discovery_service.discover_devices",
        new=AsyncMock(side_effect=AgentUnreachableError("10.0.0.31", "Cannot reach agent host 10.0.0.31: boom")),
    ):
        response = await client.post(f"/api/hosts/{host['id']}/discover")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "AGENT_UNREACHABLE"


async def test_error_envelope_for_circuit_open(client: AsyncClient) -> None:
    host = await create_host(client, **HOST_PAYLOAD)

    with patch(
        "app.routers.hosts.pack_discovery_service.discover_devices",
        new=AsyncMock(side_effect=CircuitOpenError("10.0.0.31", retry_after_seconds=12.0)),
    ):
        response = await client.post(f"/api/hosts/{host['id']}/discover")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "CIRCUIT_OPEN"
    assert response.json()["error"]["details"]["host"] == "10.0.0.31"


def test_settings_reads_phase65_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRIDFLEET_DB_POOL_SIZE", "14")
    monkeypatch.setenv("GRIDFLEET_DB_MAX_OVERFLOW", "28")
    monkeypatch.setenv("GRIDFLEET_REQUEST_TIMEOUT_SEC", "45")

    configured = Settings()

    assert configured.db_pool_size == 14
    assert configured.db_max_overflow == 28
    assert configured.request_timeout_sec == 45


def test_build_engine_uses_configured_pool_settings() -> None:
    captured: dict[str, object] = {}

    def fake_create_async_engine(url: str, **kwargs: object) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return object()

    with patch("app.database.create_async_engine", side_effect=fake_create_async_engine):
        original_pool_size = settings.db_pool_size
        original_max_overflow = settings.db_max_overflow
        try:
            settings.db_pool_size = 12
            settings.db_max_overflow = 24
            build_engine(database_url="postgresql+asyncpg://example/gridfleet")
        finally:
            settings.db_pool_size = original_pool_size
            settings.db_max_overflow = original_max_overflow

    assert captured["url"] == "postgresql+asyncpg://example/gridfleet"
    assert captured["pool_size"] == 12
    assert captured["max_overflow"] == 24
    assert captured["pool_recycle"] == 3600
    assert captured["pool_pre_ping"] is True
