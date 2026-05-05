"""Per-host httpx.AsyncClient pool.

Reduces TCP/TLS handshake cost across the leader loops and host calls. Pool
key is (host_ip, agent_port). Auth is intentionally not part of the key today;
backend->agent does not pass httpx.Auth. When machine credentials are added,
extend the key in that PR.

Pool tuning settings (max_keepalive_connections, keepalive_expiry) are tracked
per pooled client. When `_send_request` calls `get_client` with values that
differ from the existing entry, the stale client is closed and replaced. This
makes runtime changes via `agent.http_pool_max_keepalive` /
`agent.http_pool_idle_seconds` take effect without a process restart.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx

from app.observability import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class _ClientConfig:
    max_keepalive: int
    keepalive_expiry: float


@dataclass
class _PooledEntry:
    client: httpx.AsyncClient
    config: _ClientConfig


class AgentHttpPool:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], _PooledEntry] = {}
        self._lock = asyncio.Lock()

    def size(self) -> int:
        return len(self._entries)

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
        async with self._lock:
            entry = self._entries.get(key)
            if entry is not None and not entry.client.is_closed and entry.config == config:
                return entry.client

            stale_client: httpx.AsyncClient | None = None
            if entry is not None:
                stale_client = entry.client if not entry.client.is_closed else None
                if stale_client is not None:
                    logger.info(
                        "agent_http_pool_client_replaced",
                        host_ip=host_ip,
                        agent_port=agent_port,
                        old_max_keepalive=entry.config.max_keepalive,
                        new_max_keepalive=config.max_keepalive,
                        old_keepalive_expiry=entry.config.keepalive_expiry,
                        new_keepalive_expiry=config.keepalive_expiry,
                    )

            client = httpx.AsyncClient(
                timeout=timeout,
                limits=httpx.Limits(
                    max_keepalive_connections=config.max_keepalive,
                    keepalive_expiry=config.keepalive_expiry,
                ),
            )
            self._entries[key] = _PooledEntry(client=client, config=config)

        if stale_client is not None:
            try:
                await stale_client.aclose()
            except Exception:
                logger.exception("agent_http_pool_stale_close_failed")

        return client

    async def close(self) -> None:
        async with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()
        for entry in entries:
            try:
                await entry.client.aclose()
            except Exception:
                logger.exception("agent_http_pool_client_close_failed")


agent_http_pool = AgentHttpPool()
