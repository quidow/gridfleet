"""Shared concurrency primitives for per-host bounded fan-out sweeps."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TypeVar

K = TypeVar("K")


def per_key_semaphores(limit: int) -> defaultdict[K, asyncio.Semaphore]:
    """Lazily-populated ``key -> Semaphore(limit)`` map.

    Bounds concurrent work per key (typically per host) inside an ``asyncio.gather``
    fan-out so one hung host cannot stall a whole sweep. Each distinct key gets its
    own semaphore on first access.
    """
    return defaultdict(lambda: asyncio.Semaphore(limit))
