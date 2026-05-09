from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.services.heartbeat import _ping_agent
from app.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome


@pytest.mark.asyncio
async def test_ping_success_returns_payload_and_pooled_mode() -> None:
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
