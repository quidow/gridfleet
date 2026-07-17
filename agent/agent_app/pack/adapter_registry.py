"""Per-process registry of supervised adapter workers.

Workers are started by the ``PackStateLoop`` after a pack release has been
reconciled and its tarball verified. Routes and dispatch helpers look them up
by ``pack_id`` and release when any adapter hook fires.

The registry is intentionally minimal — a thread-safe dict keyed by
``(pack_id, release)`` plus a current-release pointer per ``pack_id`` —
so that multiple coroutines can read while reconciliation writes.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Set as AbstractSet

    from agent_app.pack.worker_supervisor import WorkerHandle


class AdapterRegistry:
    """Thread-safe per-process map of pack worker handles."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], WorkerHandle] = {}
        self._current_release: dict[str, str] = {}
        self._adapterless: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def set(self, pack_id: str, release: str, handle: WorkerHandle) -> None:
        with self._lock:
            self._by_key[(pack_id, release)] = handle
            self._current_release[pack_id] = release
            self._adapterless.discard((pack_id, release))

    def mark_adapterless(self, pack_id: str, release: str) -> None:
        """Record that this release's tarball ships no adapter wheel (a valid
        Tier-1 manifest-only pack) — distinct from a failed adapter load."""
        with self._lock:
            self._adapterless.add((pack_id, release))

    def is_adapterless(self, pack_id: str, release: str) -> bool:
        with self._lock:
            return (pack_id, release) in self._adapterless

    def purge_adapterless_except(self, keys: AbstractSet[tuple[str, str]]) -> None:
        """Drop adapterless marks for retired (pack, release) pairs so a
        re-uploaded release that gains a wheel is not silently skipped."""
        with self._lock:
            self._adapterless &= keys

    def get(self, pack_id: str, release: str) -> WorkerHandle | None:
        with self._lock:
            return self._by_key.get((pack_id, release))

    def get_current(self, pack_id: str) -> WorkerHandle | None:
        """Return the handle for the most recently loaded release of *pack_id*."""

        with self._lock:
            release = self._current_release.get(pack_id)
            if release is None:
                return None
            return self._by_key.get((pack_id, release))

    def has(self, pack_id: str, release: str) -> bool:
        with self._lock:
            return (pack_id, release) in self._by_key

    def remove(self, pack_id: str, release: str) -> WorkerHandle | None:
        with self._lock:
            handle = self._by_key.pop((pack_id, release), None)
            self._adapterless.discard((pack_id, release))
            if self._current_release.get(pack_id) == release:
                self._current_release.pop(pack_id, None)
            return handle

    def keys(self) -> list[tuple[str, str]]:
        with self._lock:
            return list(self._by_key)

    def pack_ids(self) -> list[str]:
        """Return all pack_ids that have a current release loaded."""
        with self._lock:
            return list(self._current_release)
