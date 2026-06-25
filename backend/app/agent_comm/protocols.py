"""Agent communication Protocol definitions.

These are the contracts that consumers of the circuit breaker depend on.
The AgentCircuitBreaker class satisfies them structurally.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CircuitBreakerProtocol(Protocol):
    async def before_request(self, host: str) -> float | None: ...
    async def record_success(self, host: str) -> None: ...
    async def record_failure(self, host: str, *, error: str) -> None: ...
    def public_snapshot(self, host: str) -> dict[str, Any]: ...
