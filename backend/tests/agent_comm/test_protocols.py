"""Verify agent_comm domain protocols."""

from __future__ import annotations

from app.agent_comm.circuit_breaker import AgentCircuitBreaker
from app.agent_comm.protocols import CircuitBreakerProtocol


def test_agent_circuit_breaker_satisfies_protocol() -> None:
    breaker = AgentCircuitBreaker()
    assert isinstance(breaker, CircuitBreakerProtocol)
