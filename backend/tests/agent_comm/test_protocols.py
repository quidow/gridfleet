"""Verify agent_comm domain protocols."""

from __future__ import annotations

from unittest.mock import AsyncMock

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.protocols import CircuitBreakerProtocol
from tests.fakes import FakeSettingsReader


def test_agent_circuit_breaker_satisfies_protocol() -> None:
    breaker = AgentCircuitBreaker(publisher=AsyncMock(), settings=FakeSettingsReader())
    assert isinstance(breaker, CircuitBreakerProtocol)
