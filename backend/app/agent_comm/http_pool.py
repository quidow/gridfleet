"""Per-host httpx.AsyncClient pool.

Reduces TCP/TLS handshake cost across the leader loops and host calls. Pool
key is (host_ip, agent_port). Auth is not part of the cache key (one pool
entry serves all credentials for a host); the BasicAuth carried by the pool
is read by callers per request.

Connection limits are fixed plumbing constants (POOL_MAX_KEEPALIVE,
POOL_KEEPALIVE_EXPIRY_SEC).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import httpx2 as httpx

from app.core.observability import get_logger

if TYPE_CHECKING:
    from app.agent_comm.config import AgentCommConfig

logger = get_logger(__name__)

# Plumbing constants (P5): connection-pool tuning, not operator policy.
# POOL_KEEPALIVE_EXPIRY_SEC must stay below the agent's keep-alive default —
# enforced by tests/contracts/test_timeout_lattice_parity.py.
POOL_MAX_KEEPALIVE = 10
POOL_KEEPALIVE_EXPIRY_SEC = 60.0


class PoolClosedError(RuntimeError):
    """Raised when get_client is called after the pool has been closed."""


class AgentHttpPool:
    def __init__(self, *, agent_auth: httpx.BasicAuth | None = None) -> None:
        self._entries: dict[tuple[str, int], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._closed: bool = False
        self._auth = agent_auth
        self._limits = httpx.Limits(
            max_keepalive_connections=POOL_MAX_KEEPALIVE,
            keepalive_expiry=POOL_KEEPALIVE_EXPIRY_SEC,
        )

    @property
    def auth(self) -> httpx.BasicAuth | None:
        return self._auth

    def size(self) -> int:
        return len(self._entries)

    async def get_client(
        self,
        host_ip: str,
        agent_port: int,
        *,
        timeout: float | int = 30,
    ) -> httpx.AsyncClient:
        key = (host_ip, agent_port)
        async with self._lock:
            if self._closed:
                raise PoolClosedError("agent_http_pool is closed")
            entry = self._entries.get(key)
            if entry is not None and not entry.is_closed:
                return entry
            client = httpx.AsyncClient(timeout=timeout, limits=self._limits)
            self._entries[key] = client
            return client

    async def reopen(self) -> None:
        async with self._lock:
            self._closed = False

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._entries.values())
            self._entries.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                logger.exception("agent_http_pool_client_close_failed")


def build_agent_basic_auth(settings: AgentCommConfig) -> httpx.BasicAuth | None:
    """Construct an httpx.BasicAuth from agent-comm settings, or None when creds are absent."""
    if settings.agent_auth_username and settings.agent_auth_password:
        return httpx.BasicAuth(settings.agent_auth_username, settings.agent_auth_password)
    return None
