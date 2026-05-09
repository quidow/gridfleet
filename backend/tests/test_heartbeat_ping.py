from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest
import structlog.testing

from app.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.services.heartbeat import _ping_agent
from app.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome


@pytest.mark.asyncio
async def test_ping_success_returns_payload_and_pooled_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_setting(key: str) -> object:
        if key == "agent.http_pool_enabled":
            return True
        raise KeyError(key)

    monkeypatch.setattr("app.services.settings_service.settings_service.get", fake_setting)
    with patch(
        "app.services.heartbeat.agent_health",
        new=AsyncMock(return_value={"status": "ok", "version": "1.2.3"}),
    ):
        result = await _ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.success
    assert result.payload == {"status": "ok", "version": "1.2.3"}
    assert result.alive is True
    assert result.client_mode is ClientMode.pooled
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_ping_success_reports_fresh_mode_when_pool_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_setting(key: str) -> object:
        if key == "agent.http_pool_enabled":
            return False
        raise KeyError(key)

    monkeypatch.setattr("app.services.agent_operations.settings_service.get", fake_setting)
    response = httpx.Response(
        200,
        json={"status": "ok"},
        request=httpx.Request("GET", "http://1.2.3.4:5100/agent/health"),
    )
    with patch("app.services.agent_operations.agent_request", new=AsyncMock(return_value=response)):
        result = await _ping_agent("1.2.3.4", 5100)

    assert result.outcome is HeartbeatOutcome.success
    assert result.client_mode is ClientMode.fresh


@pytest.mark.asyncio
async def test_ping_timeout_classified_via_transport_outcome() -> None:
    err = AgentUnreachableError(
        "1.2.3.4",
        "timed out",
        transport_outcome="timeout",
        error_category="ReadTimeout",
    )
    with patch("app.services.heartbeat.agent_health", new=AsyncMock(side_effect=err)):
        result = await _ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.timeout
    assert result.error_category == "ReadTimeout"


@pytest.mark.asyncio
async def test_ping_connect_error_classified() -> None:
    err = AgentUnreachableError(
        "1.2.3.4",
        "no route",
        transport_outcome="connect_error",
        error_category="ConnectError",
    )
    with patch("app.services.heartbeat.agent_health", new=AsyncMock(side_effect=err)):
        result = await _ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.connect_error


@pytest.mark.asyncio
async def test_ping_circuit_open_classified() -> None:
    err = CircuitOpenError("1.2.3.4", retry_after_seconds=10.0)
    with patch("app.services.heartbeat.agent_health", new=AsyncMock(side_effect=err)):
        result = await _ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.circuit_open
    assert result.client_mode is ClientMode.skipped_circuit_open


@pytest.mark.asyncio
async def test_ping_http_error_classified() -> None:
    err = AgentResponseError("1.2.3.4", "boom", http_status=503)
    with patch("app.services.heartbeat.agent_health", new=AsyncMock(side_effect=err)):
        result = await _ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.http_error
    assert result.http_status == 503


@pytest.mark.asyncio
async def test_ping_invalid_payload_classified() -> None:
    with patch("app.services.heartbeat.agent_health", new=AsyncMock(return_value=None)):
        result = await _ping_agent("1.2.3.4", 5100)
    assert result.outcome is HeartbeatOutcome.invalid_payload


def test_emit_heartbeat_log_records_full_schema() -> None:
    from app.services.heartbeat import _emit_heartbeat_log
    from app.services.heartbeat_outcomes import (
        ClientMode,
        HeartbeatOutcome,
        HeartbeatPingResult,
    )

    ping_result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=4_999,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="ReadTimeout",
    )
    with structlog.testing.capture_logs() as cap:
        _emit_heartbeat_log(
            host_id="host-uuid",
            host_ip="192.168.88.249",
            agent_port=5100,
            result=ping_result,
            leader_id="leader-uuid",
            loop_iteration=42,
        )

    record = next(e for e in cap if e.get("event") == "heartbeat_ping")
    assert record.get("host_id") == "host-uuid"
    assert record.get("host_ip") == "192.168.88.249"
    assert record.get("agent_port") == 5100
    assert record.get("outcome") == "timeout"
    assert record.get("client_mode") == "pooled"
    assert record.get("duration_ms") == 4999
    assert record.get("http_status") is None
    assert record.get("error_category") == "ReadTimeout"
    assert record.get("leader_id") == "leader-uuid"
    assert record.get("loop_iteration") == 42


def test_emit_heartbeat_log_success_renders_none_fields() -> None:
    from app.services.heartbeat import _emit_heartbeat_log
    from app.services.heartbeat_outcomes import (
        ClientMode,
        HeartbeatOutcome,
        HeartbeatPingResult,
    )

    ping_result = HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={"status": "ok"},
        duration_ms=12,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )
    with structlog.testing.capture_logs() as cap:
        _emit_heartbeat_log(
            host_id="host-uuid",
            host_ip="10.0.0.5",
            agent_port=5100,
            result=ping_result,
            leader_id="leader-uuid",
            loop_iteration=7,
        )

    record = next(e for e in cap if e.get("event") == "heartbeat_ping")
    assert record.get("outcome") == "success"
    assert record.get("http_status") == 200
    assert record.get("error_category") is None
