"""Per-process registry of loaded ``DriverPackAdapter`` instances.

Adapters are loaded by the ``PackStateLoop`` after a pack release has
been reconciled and its tarball verified. Routes and dispatch helpers
look them up by ``pack_id`` (current release) when ``discovery.kind ==
"adapter"`` or any adapter hook fires.

The registry is intentionally minimal — a thread-safe dict keyed by
``(pack_id, release)`` plus a current-release pointer per ``pack_id`` —
so that multiple coroutines can read while reconciliation writes.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_app.pack.adapter_types import DriverPackAdapter


class AdapterRegistry:
    """Thread-safe per-process map of loaded driver-pack adapters."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], DriverPackAdapter] = {}
        self._current_release: dict[str, str] = {}
        self._lock = threading.Lock()

    def set(self, pack_id: str, release: str, adapter: DriverPackAdapter) -> None:
        with self._lock:
            self._by_key[(pack_id, release)] = adapter
            self._current_release[pack_id] = release

    def get(self, pack_id: str, release: str) -> DriverPackAdapter | None:
        with self._lock:
            return self._by_key.get((pack_id, release))

    def get_current(self, pack_id: str) -> DriverPackAdapter | None:
        """Return the adapter for the most recently loaded release of *pack_id*."""

        with self._lock:
            release = self._current_release.get(pack_id)
            if release is None:
                return None
            return self._by_key.get((pack_id, release))

    def has(self, pack_id: str, release: str) -> bool:
        with self._lock:
            return (pack_id, release) in self._by_key

    def clear(self) -> None:
        with self._lock:
            self._by_key.clear()
            self._current_release.clear()
