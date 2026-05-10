"""Process-wide shared httpx.AsyncClient for the agent.

Per-call instantiation of `httpx.AsyncClient` leaks ~0.8 MB of native state
on macOS (TLS contexts, anyio sync primitives, certifi parse cache) — the
allocator does not return that memory even after `aclose()`. The agent runs
several periodic call sites (manager registration heartbeat, Appium probe,
driver-pack tarball download) so we share a single client and close it on
shutdown.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """Return the process-wide shared client, creating it on first use."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient()
    return _client


async def close() -> None:
    """Close the shared client. Call from FastAPI lifespan shutdown."""
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
