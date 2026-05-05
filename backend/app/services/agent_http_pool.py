"""Per-host httpx.AsyncClient pool.

Reduces TCP/TLS handshake cost across the leader loops and host calls. Pool
key is (host_ip, agent_port). Auth is intentionally not part of the key today;
backend->agent does not pass httpx.Auth. When machine credentials are added,
extend the key in that PR.
"""

from __future__ import annotations

import asyncio

import httpx

from app.observability import get_logger

logger = get_logger(__name__)


class AgentHttpPool:
    def __init__(self) -> None:
        self._clients: dict[tuple[str, int], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    def size(self) -> int:
        return len(self._clients)

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
        async with self._lock:
            client = self._clients.get(key)
            if client is None or client.is_closed:
                client = httpx.AsyncClient(
                    timeout=timeout,
                    limits=httpx.Limits(
                        max_keepalive_connections=max_keepalive,
                        keepalive_expiry=float(keepalive_expiry),
                    ),
                )
                self._clients[key] = client
            return client

    async def close(self) -> None:
        async with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                await client.aclose()
            except Exception:
                logger.exception("agent_http_pool_client_close_failed")


agent_http_pool = AgentHttpPool()
