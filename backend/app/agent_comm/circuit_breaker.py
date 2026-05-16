from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any

from sqlalchemy import or_, select

from app.core.database import async_session
from app.core.observability import get_logger
from app.events import event_bus
from app.hosts.models.host import Host
from app.settings import settings_service

logger = get_logger(__name__)


async def _resolve_host_identity(host_addr: str) -> dict[str, str]:
    """Resolve an IP or hostname to ``{"host_id": ..., "hostname": ...}``.

    Returns an empty dict when the DB is unreachable or no row matches. The
    breaker must keep emitting even without a resolvable Host row so that
    pre-registration failures still surface in the global event stream.
    """
    try:
        async with async_session() as db:
            stmt = select(Host.id, Host.hostname).where(or_(Host.ip == host_addr, Host.hostname == host_addr)).limit(1)
            row = (await db.execute(stmt)).first()
            if row is None:
                return {}
            return {"host_id": str(row[0]), "hostname": str(row[1])}
    except Exception:  # noqa: BLE001 - enrichment must never break the breaker
        logger.debug("circuit_breaker_host_lookup_failed", host=host_addr, exc_info=True)
        return {}


@dataclass
class CircuitState:
    status: str = "closed"
    consecutive_failures: int = 0
    opened_until: float | None = None
    probe_in_flight: bool = False
    last_error: str | None = None


class AgentCircuitBreaker:
    def __init__(self) -> None:
        self._states: dict[str, CircuitState] = {}
        self._lock = asyncio.Lock()

    def _failure_threshold(self) -> int:
        return int(settings_service.get("agent.circuit_breaker_failure_threshold"))

    def _cooldown_seconds(self) -> float:
        return float(settings_service.get("agent.circuit_breaker_cooldown_seconds"))

    def failure_threshold(self) -> int:
        """Public read accessor for the current failure threshold (reads from settings)."""
        return self._failure_threshold()

    def cooldown_seconds(self) -> float:
        """Public read accessor for the current cooldown duration (reads from settings)."""
        return self._cooldown_seconds()

    async def before_request(self, host: str) -> float | None:
        async with self._lock:
            state = self._states.setdefault(host, CircuitState())
            now = monotonic()

            if state.status == "open":
                if state.opened_until is not None and now >= state.opened_until:
                    state.status = "half_open"
                    state.probe_in_flight = True
                    return None
                retry_after = (state.opened_until or now) - now
                return max(0.0, retry_after)

            if state.status == "half_open":
                if state.probe_in_flight:
                    return 0.0
                state.probe_in_flight = True
                return None

            return None

    async def record_success(self, host: str) -> None:
        publish_closed = False
        async with self._lock:
            state = self._states.setdefault(host, CircuitState())
            publish_closed = state.status != "closed"
            state.status = "closed"
            state.consecutive_failures = 0
            state.opened_until = None
            state.probe_in_flight = False
            state.last_error = None

        if publish_closed:
            logger.info("Agent circuit breaker closed", host=host)
            payload: dict[str, Any] = {"host": host, **(await _resolve_host_identity(host))}
            await event_bus.publish(
                "host.circuit_breaker.closed",
                payload,
            )

    async def record_failure(self, host: str, *, error: str) -> None:
        publish_opened = False
        failure_count = 0
        threshold = self._failure_threshold()
        cooldown = self._cooldown_seconds()
        async with self._lock:
            state = self._states.setdefault(host, CircuitState())
            state.last_error = error
            now = monotonic()

            if state.status == "half_open":
                state.status = "open"
                state.opened_until = now + cooldown
                state.probe_in_flight = False
                state.consecutive_failures = threshold
                publish_opened = True
            elif state.status == "open":
                state.opened_until = now + cooldown
                state.probe_in_flight = False
            else:
                state.consecutive_failures += 1
                if state.consecutive_failures >= threshold:
                    state.status = "open"
                    state.opened_until = now + cooldown
                    state.probe_in_flight = False
                    publish_opened = True

            failure_count = state.consecutive_failures

        if publish_opened:
            logger.warning(
                "Agent circuit breaker opened",
                host=host,
                consecutive_failures=failure_count,
                cooldown_seconds=cooldown,
                error=error,
            )
            payload: dict[str, Any] = {
                "host": host,
                "consecutive_failures": failure_count,
                "cooldown_seconds": cooldown,
                "last_error": error,
                **(await _resolve_host_identity(host)),
            }
            await event_bus.publish(
                "host.circuit_breaker.opened",
                payload,
            )

    def reset(self) -> None:
        self._states.clear()

    def snapshot(self, host: str) -> dict[str, str | int | float | None]:
        state = self._states.get(host, CircuitState())
        return {
            "status": state.status,
            "consecutive_failures": state.consecutive_failures,
            "opened_until": state.opened_until,
            "probe_in_flight": state.probe_in_flight,
            "last_error": state.last_error,
        }

    def public_snapshot(self, host: str) -> dict[str, Any]:
        state = self._states.get(host, CircuitState())
        retry_after_seconds: float | None = None
        if state.status == "open" and state.opened_until is not None:
            retry_after_seconds = max(0.0, state.opened_until - monotonic())
        return {
            "status": state.status,
            "consecutive_failures": state.consecutive_failures,
            "cooldown_seconds": self._cooldown_seconds(),
            "retry_after_seconds": retry_after_seconds,
            "probe_in_flight": state.probe_in_flight,
            "last_error": state.last_error,
        }


agent_circuit_breaker = AgentCircuitBreaker()
