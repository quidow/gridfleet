from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from sqlalchemy import select

from app import agent_client, database, health, metrics
from app.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.models.control_plane_state_entry import ControlPlaneStateEntry
from app.services import control_plane_state_store, device_identity, grid_service, run_reaper
from app.shutdown import ShutdownCoordinator
from app.type_defs import AsyncSessionContextManager, SessionFactory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pytest import MonkeyPatch
    from sqlalchemy.ext.asyncio import AsyncSession


class DummyAgentClient:
    def __init__(self, *, response: httpx.Response | None = None, error: Exception | None = None) -> None:
        self.response = response or httpx.Response(200, request=httpx.Request("GET", "http://example.test"), json={})
        self.error = error

    async def __aenter__(self) -> DummyAgentClient:
        return self

    async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
        return False

    async def get(self, url: str, **kwargs: object) -> httpx.Response:
        if self.error is not None:
            raise self.error
        return self.response


def test_request_kwargs_and_build_agent_headers() -> None:
    kwargs = agent_client._request_kwargs(
        "get",
        headers={"X-Test": "1"},
        params={"a": "b"},
        json_body={"ignored": True},
        timeout=5,
    )
    assert kwargs == {"headers": {"X-Test": "1"}, "params": {"a": "b"}, "timeout": 5}
    assert agent_client.build_agent_headers({"X-Test": "1"})["X-Test"] == "1"


async def test_agent_client_request_handles_circuit_open_and_transport_errors(monkeypatch: MonkeyPatch) -> None:
    request = httpx.Request("GET", "http://10.0.0.5:5100/agent/health")
    monkeypatch.setattr("app.agent_client.record_agent_call", Mock())
    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.before_request", AsyncMock(return_value=1.5))

    with pytest.raises(CircuitOpenError):
        await agent_client.request("GET", "http://10.0.0.5:5100/agent/health", endpoint="health", host="10.0.0.5")

    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.before_request", AsyncMock(return_value=None))
    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.record_failure", AsyncMock())
    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.record_success", AsyncMock())
    client = DummyAgentClient(error=httpx.ConnectError("boom", request=request))

    with pytest.raises(AgentUnreachableError, match="Cannot reach agent host"):
        await agent_client.request(
            "GET",
            "http://10.0.0.5:5100/agent/health",
            endpoint="health",
            host="10.0.0.5",
            client=client,
        )


async def test_agent_client_request_uses_owned_client_factory(monkeypatch: MonkeyPatch) -> None:
    response = httpx.Response(200, request=httpx.Request("GET", "http://10.0.0.5:5100/agent/health"), json={"ok": True})
    monkeypatch.setattr("app.agent_client.record_agent_call", Mock())
    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.before_request", AsyncMock(return_value=None))
    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.record_failure", AsyncMock())
    monkeypatch.setattr("app.agent_client.agent_circuit_breaker.record_success", AsyncMock())

    result = await agent_client.request(
        "GET",
        "http://10.0.0.5:5100/agent/health",
        endpoint="health",
        host="10.0.0.5",
        client_factory=lambda: DummyAgentClient(response=response),
    )

    assert result.status_code == 200


async def test_check_readiness_short_circuits_when_shutting_down(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(health.shutdown_coordinator, "is_shutting_down", lambda: True)
    monkeypatch.setattr(health.shutdown_coordinator, "active_requests", lambda: 2)

    payload, status = await health.check_readiness(AsyncMock())

    assert status == 503
    assert payload["checks"]["shutdown"]["shutting_down"] is True


async def test_check_readiness_marks_unhealthy_stale_loop(monkeypatch: MonkeyPatch) -> None:
    db = AsyncMock()
    monkeypatch.setattr(health.shutdown_coordinator, "is_shutting_down", lambda: False)
    monkeypatch.setattr(health.shutdown_coordinator, "active_requests", lambda: 0)
    monkeypatch.setattr(
        "app.health.get_background_loop_snapshots",
        AsyncMock(return_value={"heartbeat": {"owner": "x"}}),
    )
    monkeypatch.setattr("app.health.loop_heartbeat_fresh", lambda snapshot, now: False)

    payload, status = await health.check_readiness(db)

    assert status == 503
    assert payload["checks"]["control_plane_leader"] is False


def test_metrics_helpers_and_rendering() -> None:
    metrics.record_background_loop_error("heartbeat", 0.2)
    metrics.record_webhook_delivery("success", count=0)
    metrics.record_event_published("device.created")
    assert isinstance(metrics.render_metrics(), bytes)


async def test_get_db_uses_async_session_context(monkeypatch: MonkeyPatch) -> None:
    yielded = object()

    @asynccontextmanager
    async def fake_async_session() -> AsyncGenerator[object, None]:
        yield yielded

    # Phase 0b: database is a shim re-exporting from app.core.database; the
    # canonical async_session lookup happens in app.core.database.
    from app.core import database as core_database

    monkeypatch.setattr(core_database, "async_session", fake_async_session)
    monkeypatch.setattr(database, "async_session", fake_async_session)

    generator = database.get_db()
    assert await anext(generator) is yielded
    with pytest.raises(StopAsyncIteration):
        await anext(generator)


async def test_run_reaper_loop_logs_initial_failure_and_retries() -> None:
    class _Observation:
        @asynccontextmanager
        async def cycle(self) -> AsyncGenerator[AsyncMock, None]:
            yield AsyncMock()

    @asynccontextmanager
    async def fake_session() -> AsyncGenerator[AsyncMock, None]:
        yield AsyncMock()

    with (
        patch("app.services.run_reaper.observe_background_loop", return_value=_Observation()),
        patch("app.services.run_reaper.async_session", fake_session),
        patch(
            "app.services.run_reaper._reap_stale_runs",
            new=AsyncMock(side_effect=[RuntimeError("boom"), RuntimeError("boom-again"), asyncio.CancelledError()]),
        ),
        patch("app.services.run_reaper.settings_service.get", return_value=1),
        patch("app.services.run_reaper.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await run_reaper.run_reaper_loop()

    sleep.assert_awaited()


async def test_control_plane_state_store_round_trip(db_session: AsyncSession) -> None:
    await control_plane_state_store.set_many(db_session, "empty", {})
    await control_plane_state_store.set_value(db_session, "demo", "a", {"x": 1})
    assert await control_plane_state_store.get_value(db_session, "demo", "a") == {"x": 1}
    await control_plane_state_store.set_many(db_session, "demo", {"b": 2, "c": 3})
    assert await control_plane_state_store.get_values(db_session, "demo", keys=[]) == {}
    assert await control_plane_state_store.get_values(db_session, "demo", keys=["a", "b"]) == {"a": {"x": 1}, "b": 2}
    assert await control_plane_state_store.try_claim_value(db_session, "demo", "claimed", 1) is True
    assert await control_plane_state_store.try_claim_value(db_session, "demo", "claimed", 2) is False
    assert await control_plane_state_store.increment_counter(db_session, "demo", "counter", 2) == 2
    await control_plane_state_store.delete_value(db_session, "demo", "a")
    await control_plane_state_store.delete_namespaces(db_session, [])
    await control_plane_state_store.delete_namespaces(db_session, ["demo"])
    await db_session.commit()
    remaining = await db_session.execute(
        select(ControlPlaneStateEntry).where(ControlPlaneStateEntry.namespace == "demo")
    )
    assert remaining.scalars().all() == []


async def test_type_def_protocol_defaults_raise() -> None:
    with pytest.raises(NotImplementedError):
        await agent_client.AgentHttpClient.__aenter__(object())
    with pytest.raises(NotImplementedError):
        await agent_client.AgentHttpClient.__aexit__(object(), None, None, None)
    with pytest.raises(NotImplementedError):
        await agent_client.AgentHttpClient.get(object(), "http://agent")
    with pytest.raises(NotImplementedError):
        await agent_client.AgentHttpClient.post(object(), "http://agent")
    with pytest.raises(NotImplementedError):
        await AsyncSessionContextManager.__aenter__(object())
    with pytest.raises(NotImplementedError):
        await AsyncSessionContextManager.__aexit__(object(), None, None, None)
    with pytest.raises(NotImplementedError):
        SessionFactory.__call__(object())


def test_error_detail_and_transport_helpers() -> None:
    response_error = AgentResponseError("10.0.0.1", "bad", http_status=503, details={"extra": True})
    assert response_error.http_status == 503
    assert response_error.details["http_status"] == 503
    assert response_error.details["extra"] is True

    assert agent_client.classify_httpx_transport(httpx.TimeoutException("slow"))[0] == "timeout"
    assert agent_client.classify_httpx_transport(httpx.ConnectError("Name resolution failed"))[0] == "dns_error"
    assert agent_client.classify_httpx_transport(httpx.ConnectError("refused"))[0] == "connect_error"
    assert agent_client.classify_httpx_transport(RuntimeError("boom"))[0] == "unexpected_error"


def test_grid_status_device_ids_ignore_malformed_nodes() -> None:
    assert grid_service.available_node_device_ids({"value": None}) is None
    assert grid_service.available_node_device_ids({"value": {"nodes": None}}) is None
    assert grid_service.available_node_device_ids(
        {
            "value": {
                "nodes": [
                    "bad-node",
                    {"availability": "DOWN", "slots": [{"stereotype": {"gridfleet:DeviceId": "ignored"}}]},
                    {"slots": "bad-slots"},
                    {"slots": ["bad-slot", {"stereotype": "bad-stereotype"}]},
                    {"slots": [{"stereotype": {"gridfleet:deviceId": "device-1"}}]},
                    {"slots": [{"stereotype": {"appium:gridfleet:deviceId": "device-2"}}]},
                ]
            }
        }
    ) == {"device-1", "device-2"}


async def test_grid_service_reuses_and_closes_shared_client(monkeypatch: MonkeyPatch) -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.is_closed = False
            self.closed = False

        async def aclose(self) -> None:
            self.closed = True
            self.is_closed = True

    created: list[FakeClient] = []

    def fake_client_factory() -> FakeClient:
        client = FakeClient()
        created.append(client)
        return client

    await grid_service.close()
    monkeypatch.setattr("app.services.grid_service.httpx.AsyncClient", fake_client_factory)

    first = grid_service._get_client()
    second = grid_service._get_client()
    assert first is second
    await grid_service.close()
    assert created == [first]
    assert first.closed is True


async def test_shutdown_coordinator_idempotent_paths() -> None:
    coordinator = ShutdownCoordinator()
    coordinator.request_finished()
    assert coordinator.active_requests() == 0

    coordinator.request_started()
    await coordinator.begin_shutdown()
    await coordinator.begin_shutdown()
    assert coordinator.is_shutting_down() is True
    assert await coordinator.wait_for_drain(timeout=0.001) is False

    coordinator.request_finished()
    assert await coordinator.wait_for_drain(timeout=0.1) is True
    coordinator.reset()
    assert coordinator.is_shutting_down() is False
    assert coordinator.active_requests() == 0


def test_device_identity_helpers_cover_host_port_and_fallbacks() -> None:
    assert device_identity.looks_like_ip_address(None) is False
    assert device_identity.looks_like_ip_address("not-an-ip") is False
    assert device_identity.looks_like_ip_address("10.0.0.5") is True
    assert device_identity.parse_ip_from_connection_target(None) is None
    assert device_identity.parse_ip_from_connection_target("10.0.0.5:5555") == "10.0.0.5"
    assert device_identity.parse_ip_from_connection_target("10.0.0.6") == "10.0.0.6"
    assert device_identity.parse_ip_from_connection_target("serial:5555") is None
    assert device_identity.looks_like_ip_port_target(None) is False
    assert device_identity.looks_like_ip_port_target("10.0.0.5:5555") is True
    assert device_identity.looks_like_ip_port_target("10.0.0.5:abc") is False
    assert device_identity.is_host_scoped_identity(identity_scope="host") is True

    assert (
        device_identity.appium_connection_target(SimpleNamespace(connection_target="tcp", identity_value="id")) == "tcp"
    )
    assert device_identity.appium_connection_target(SimpleNamespace(connection_target="", identity_value="id")) == "id"
    with pytest.raises(ValueError, match="no connection target"):
        device_identity.appium_connection_target(SimpleNamespace(connection_target="", identity_value=""))

    assert device_identity.derive_pack_identity(
        identity_scheme="scheme",
        identity_scope="host",
        identity_value=None,
        connection_target=None,
        ip_address="10.0.0.7",
    ) == ("scheme", "host", "10.0.0.7", "10.0.0.7", "10.0.0.7")
