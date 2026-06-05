"""Per-host cache of the hub's ``GET /status`` node list.

Every node's heartbeat asks the hub whether it is still registered, but the
hub's ``/status`` payload already lists *all* nodes — with N nodes per host
that is N identical requests every heartbeat. One cached fetch per TTL
answers every node's probe, keeping hub traffic flat in the node count.
Failures are cached too (as ``None``) so a hub outage costs one failed
request per TTL instead of N.
"""

from __future__ import annotations

from typing import Any

import httpx

from agent_app import http_client
from agent_app.async_ttl_cache import AsyncTTLCache

# 0.8 x the default 5 s grid-node heartbeat: each heartbeat sees data at most
# one interval old while all node probes within a tick share one fetch.
_TTL_SECONDS = 4.0

_cache: AsyncTTLCache[str, list[dict[str, Any]] | None] = AsyncTTLCache(ttl_seconds=_TTL_SECONDS)


async def _fetch_hub_nodes(hub_status_url: str) -> list[dict[str, Any]] | None:
    url = hub_status_url.rstrip("/") + "/status"
    try:
        resp = await http_client.get_client().get(url, timeout=5.0)
        if resp.status_code != 200:
            return None
        nodes = (resp.json().get("value") or {}).get("nodes") or []
    except (httpx.HTTPError, ValueError):
        return None
    return [node for node in nodes if isinstance(node, dict)]


async def get_hub_nodes(hub_status_url: str, *, fresh: bool = False) -> list[dict[str, Any]] | None:
    """Return the hub's node list, or ``None`` when the hub is unreachable/unparseable.

    ``fresh=True`` bypasses the cached snapshot (used to confirm a node's
    absence definitively — a cached snapshot may predate the node's own
    NODE_ADDED) and replaces the cache entry for subsequent callers.
    """
    if fresh:
        nodes = await _fetch_hub_nodes(hub_status_url)
        _cache.put(hub_status_url, nodes)
        return nodes

    async def _fetch() -> list[dict[str, Any]] | None:
        return await _fetch_hub_nodes(hub_status_url)

    return await _cache.get(hub_status_url, _fetch)


def clear() -> None:
    """Test hook: drop all cached entries."""
    _cache.clear()
