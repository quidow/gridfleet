"""Agent communication service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol


@dataclass(frozen=True, slots=True)
class AgentCommServices:
    http_pool: AgentHttpPool
    circuit_breaker: CircuitBreakerProtocol
