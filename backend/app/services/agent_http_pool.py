"""Per-host httpx.AsyncClient pool.

Reduces TCP/TLS handshake cost across the leader loops and host calls. Pool
key is (host_ip, agent_port). Auth is intentionally not part of the key today;
backend->agent does not pass httpx.Auth. When machine credentials are added,
extend the key in that PR.

Pool tuning settings (max_keepalive_connections, keepalive_expiry) are tracked
per pooled client. When `_send_request` calls `get_client` with values that
differ from the existing entry, the live entry is replaced and the previous
client is moved into a bounded deferred-close list with a creation timestamp.
The stale client is NOT aclose()d immediately, because in-flight requests
obtained their reference before the replacement and would otherwise fail with
a closed-client error mid-flight (httpx raises during the response read).

Deferred clients are drained when:
  1. their grace window has elapsed (default DEFERRED_GRACE_SECONDS); by then
     any concurrent request that obtained the old client has long since
     completed or hit its own request timeout, so aclose() is safe;
  2. the deferred list exceeds DEFERRED_MAX, in which case the oldest entries
     are aclose()d immediately (FIFO eviction) regardless of age — this
     bounds memory under pathological tuning loops;
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

from app.observability import get_logger

logger = get_logger(__name__)

# Time after which a deferred (stale) client is safe to aclose(). Must be
# greater than the longest realistic in-flight request, including the
# request timeout (30s) plus the agent's response time. 90s is conservative.
DEFERRED_GRACE_SECONDS: float = 90.0

# Hard cap on outstanding deferred clients. If exceeded, oldest entries are
# aclose()d immediately. Tuning changes are an operator action that fires at
# most a handful of times per process; a cap of 32 absorbs reasonable activity
# while preventing leaks under a stuck-loop misconfiguration.
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


@dataclass
class _DeferredEntry:
    client: httpx.AsyncClient
    deferred_at: float


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

        # Collect anything that needs aclose()ing outside the lock so we
        # do not block other callers on network teardown work.
        to_close: list[httpx.AsyncClient] = []
        async with self._lock:
            if self._closed:
                raise PoolClosedError("agent_http_pool is closed")

            entry = self._entries.get(key)
            if entry is not None and not entry.client.is_closed and entry.config == config:
                return entry.client

            # Phase 1: drain entries past the grace window (from previous
            # cycles only — a freshly stale entry is appended below).
            to_close = self._collect_grace_drainable_locked()

            if entry is not None and not entry.client.is_closed:
                self._deferred.append(_DeferredEntry(client=entry.client, deferred_at=time.monotonic()))
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

            # Phase 2: enforce hard cap AFTER the new stale is recorded.
            # Oldest entries beyond DEFERRED_MAX are evicted FIFO.
            to_close.extend(self._enforce_cap_locked())

            client = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_keepalive_connections=config.max_keepalive,
                    keepalive_expiry=config.keepalive_expiry,
                ),
            )
            self._entries[key] = _PooledEntry(client=client, config=config)

        for stale in to_close:
            try:
                await stale.aclose()
            except Exception:
                logger.exception("agent_http_pool_deferred_drain_failed")

        return client

    def _collect_grace_drainable_locked(self) -> list[httpx.AsyncClient]:
        """Return deferred clients past the grace window. Caller holds the lock."""
        now = time.monotonic()
        drainable: list[httpx.AsyncClient] = []
        keep: list[_DeferredEntry] = []
        for d in self._deferred:
            if now - d.deferred_at >= DEFERRED_GRACE_SECONDS:
                drainable.append(d.client)
            else:
                keep.append(d)
        self._deferred = keep
        return drainable

    def _enforce_cap_locked(self) -> list[httpx.AsyncClient]:
        """Evict oldest deferred entries while count > DEFERRED_MAX. Locked."""
        if len(self._deferred) <= DEFERRED_MAX:
            return []
        overflow = len(self._deferred) - DEFERRED_MAX
        evicted = [d.client for d in self._deferred[:overflow]]
        now = time.monotonic()
        for d in self._deferred[:overflow]:
            logger.warning(
                "agent_http_pool_deferred_evicted",
                reason="deferred_max_exceeded",
                age_sec=now - d.deferred_at,
            )
        self._deferred = self._deferred[overflow:]
        return evicted

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
