from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from sqlalchemy import select

from app import agent_client, database, health, metrics
from app.errors import AgentUnreachableError, CircuitOpenError
from app.models.control_plane_state_entry import ControlPlaneStateEntry
from app.services import control_plane_state_store, run_reaper

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
