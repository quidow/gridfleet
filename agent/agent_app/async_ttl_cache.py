"""Single-flight TTL cache for async fetches.

Used by periodic call sites that would otherwise issue the same upstream
request once per node/device (hub status probes, discovery sweep fallbacks):
concurrent callers for the same key await one fetch, and the result —
including a failed-fetch ``None`` — is served from cache until the TTL
expires.

Keys must come from a bounded set (URLs, pack tuples): per-key locks are kept
for the cache's lifetime and are only dropped by ``clear()``. A caller keying
on an unbounded space (device ids, connection targets) would leak one Lock per
distinct key — use a ref-counted helper like ``pack.tarball_fetch`` instead.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Hashable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class AsyncTTLCache[K: Hashable, V]:
    def __init__(self, *, ttl_seconds: float, now: Callable[[], float] = time.monotonic) -> None:
        self._ttl_seconds = ttl_seconds
        self._now = now
        self._entries: dict[K, tuple[float, V]] = {}
        self._locks: dict[K, asyncio.Lock] = {}

    async def get(self, key: K, fetch: Callable[[], Awaitable[V]]) -> V:
        """Return the cached value for ``key``, fetching (single-flight) when absent or expired."""
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            entry = self._entries.get(key)
            if entry is not None and self._now() < entry[0]:
                return entry[1]
            value = await fetch()
            self._entries[key] = (self._now() + self._ttl_seconds, value)
            return value

    def put(self, key: K, value: V) -> None:
        """Store ``value`` for ``key`` directly, starting a fresh TTL window."""
        self._entries[key] = (self._now() + self._ttl_seconds, value)

    def clear(self) -> None:
        """Drop all entries (test hook / explicit invalidation)."""
        self._entries.clear()
        self._locks.clear()
