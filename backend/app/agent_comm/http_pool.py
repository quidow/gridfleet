"""Per-host httpx.AsyncClient pool.

Reduces TCP/TLS handshake cost across the leader loops and host calls. Pool
key is (host_ip, agent_port). Auth is intentionally not part of the key today;
backend->agent does not pass httpx.Auth. When machine credentials are added,
extend the key in that PR.

Pool tuning settings (max_keepalive_connections, keepalive_expiry) and the
largest request timeout served by a pooled client are tracked per entry. When
`_send_request` calls `get_client` with values that differ from the existing
entry, the live entry is replaced and the previous client is moved into a
bounded deferred-close list with the earliest safe close timestamp.
The stale client is NOT aclose()d immediately, because in-flight requests
obtained their reference before the replacement and would otherwise fail with
a closed-client error mid-flight (httpx raises during the response read).

Deferred clients are drained when:
  1. their safe-close timestamp has elapsed; this is the larger of
     DEFERRED_GRACE_SECONDS and the max request timeout observed while the
     client was active;
  2. the deferred list exceeds DEFERRED_MAX and old entries are already safe to
     close, in which case they are aclose()d FIFO to trim the list;
  3. the pool is being shut down via close().

This makes runtime changes via `agent.http_pool_max_keepalive` /
`agent.http_pool_idle_seconds` take effect without a process restart, while
keeping concurrent in-flight requests safe and preventing unbounded growth.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from app.core.observability import get_logger

logger = get_logger(__name__)

# Minimum time after which a deferred (stale) client is safe to aclose().
# Each pooled entry also tracks the largest per-request timeout it served; the
# actual safe-close delay is max(DEFERRED_GRACE_SECONDS, observed_timeout).
DEFERRED_GRACE_SECONDS: float = 90.0

# Soft cap on outstanding deferred clients. If exceeded, oldest entries that
# are already past their safe-close timestamp are aclose()d. Unsafe entries are
# left open so tuning changes cannot break in-flight requests.
DEFERRED_MAX: int = 32


class PoolClosedError(RuntimeError):
    """Raised when get_client is called after the pool has been closed."""


@dataclass(frozen=True)
class _ClientConfig:
    max_keepalive: int
    keepalive_expiry: float


@dataclass
class _PooledEntry:
    client: httpx.AsyncClient
    config: _ClientConfig
    max_timeout: float


@dataclass
class _DeferredEntry:
    client: httpx.AsyncClient
    deferred_at: float
    close_after: float


def _timeout_seconds(timeout: float | int) -> float:
    return float(timeout)


class AgentHttpPool:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], _PooledEntry] = {}
        self._deferred: list[_DeferredEntry] = []
        self._lock = asyncio.Lock()
        self._closed: bool = False

    def size(self) -> int:
        return len(self._entries)

    def deferred_count(self) -> int:
        return len(self._deferred)

    async def get_client(
        self,
        host_ip: str,
        agent_port: int,
        *,
        timeout: float | int = 30,
        max_keepalive: int = 10,
        keepalive_expiry: float | int = 60,
    ) -> httpx.AsyncClient:
        key = (host_ip, agent_port)
        config = _ClientConfig(
            max_keepalive=int(max_keepalive),
            keepalive_expiry=float(keepalive_expiry),
        )
        timeout_seconds = _timeout_seconds(timeout)

        # Collect anything that needs aclose()ing outside the lock so we
        # do not block other callers on network teardown work.
        to_close: list[httpx.AsyncClient] = []
        async with self._lock:
            if self._closed:
                raise PoolClosedError("agent_http_pool is closed")

            now = time.monotonic()
            to_close = self._collect_closeable_locked(now)
            entry = self._entries.get(key)
            if entry is not None and not entry.client.is_closed and entry.config == config:
                entry.max_timeout = max(entry.max_timeout, timeout_seconds)
                client = entry.client
            else:
                if entry is not None and not entry.client.is_closed:
                    close_after = now + max(DEFERRED_GRACE_SECONDS, entry.max_timeout)
                    self._deferred.append(_DeferredEntry(client=entry.client, deferred_at=now, close_after=close_after))
                    logger.info(
                        "agent_http_pool_client_replaced",
                        host_ip=host_ip,
                        agent_port=agent_port,
                        old_max_keepalive=entry.config.max_keepalive,
                        new_max_keepalive=config.max_keepalive,
                        old_keepalive_expiry=entry.config.keepalive_expiry,
                        new_keepalive_expiry=config.keepalive_expiry,
                        deferred_count=len(self._deferred),
                    )

                # Trim only stale entries that are already safe to close.
                to_close.extend(self._enforce_cap_locked(now))

                client = httpx.AsyncClient(
                    timeout=timeout,
                    limits=httpx.Limits(
                        max_keepalive_connections=config.max_keepalive,
                        keepalive_expiry=config.keepalive_expiry,
                    ),
                )
                self._entries[key] = _PooledEntry(client=client, config=config, max_timeout=timeout_seconds)

        for stale in to_close:
            try:
                await stale.aclose()
            except Exception:
                logger.exception("agent_http_pool_deferred_drain_failed")

        return client

    def _collect_closeable_locked(self, now: float) -> list[httpx.AsyncClient]:
        """Return deferred clients past their safe-close timestamp. Locked."""
        drainable: list[httpx.AsyncClient] = []
        keep: list[_DeferredEntry] = []
        for d in self._deferred:
            if now >= d.close_after:
                drainable.append(d.client)
            else:
                keep.append(d)
        self._deferred = keep
        return drainable

    def _enforce_cap_locked(self, now: float) -> list[httpx.AsyncClient]:
        """Evict oldest safe deferred entries while count > DEFERRED_MAX. Locked."""
        if len(self._deferred) <= DEFERRED_MAX:
            return []

        overflow = len(self._deferred) - DEFERRED_MAX
        evicted: list[httpx.AsyncClient] = []
        keep: list[_DeferredEntry] = []
        for d in self._deferred:
            if overflow > 0 and now >= d.close_after:
                evicted.append(d.client)
                overflow -= 1
                logger.warning(
                    "agent_http_pool_deferred_evicted",
                    reason="deferred_max_exceeded",
                    age_sec=now - d.deferred_at,
                )
            else:
                keep.append(d)
        self._deferred = keep
        if overflow > 0:
            logger.warning(
                "agent_http_pool_deferred_over_cap",
                reason="deferred_entries_not_yet_safe_to_close",
                deferred_count=len(self._deferred),
                deferred_max=DEFERRED_MAX,
                unsafe_overflow=overflow,
            )
        return evicted

    async def reopen(self) -> None:
        async with self._lock:
            self._closed = False

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            entries = list(self._entries.values())
            deferred = [d.client for d in self._deferred]
            self._entries.clear()
            self._deferred.clear()
        for entry in entries:
            try:
                await entry.client.aclose()
            except Exception:
                logger.exception("agent_http_pool_client_close_failed")
        for stale in deferred:
            try:
                await stale.aclose()
            except Exception:
                logger.exception("agent_http_pool_deferred_close_failed")


agent_http_pool = AgentHttpPool()
