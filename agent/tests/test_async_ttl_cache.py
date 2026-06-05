from __future__ import annotations

import asyncio

import pytest

from agent_app.async_ttl_cache import AsyncTTLCache


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _Counter:
    def __init__(self, value: object = "v") -> None:
        self.calls = 0
        self.value = value

    async def fetch(self) -> object:
        self.calls += 1
        await asyncio.sleep(0)  # let concurrent waiters pile up on the lock
        return self.value


@pytest.mark.asyncio
async def test_concurrent_gets_share_one_fetch() -> None:
    cache: AsyncTTLCache[str, object] = AsyncTTLCache(ttl_seconds=10.0)
    counter = _Counter()
    results = await asyncio.gather(*(cache.get("k", counter.fetch) for _ in range(5)))
    assert counter.calls == 1
    assert all(r == "v" for r in results)


@pytest.mark.asyncio
async def test_value_cached_until_ttl_expires() -> None:
    clock = _Clock()
    cache: AsyncTTLCache[str, object] = AsyncTTLCache(ttl_seconds=4.0, now=clock)
    counter = _Counter()
    await cache.get("k", counter.fetch)
    clock.t = 3.9
    await cache.get("k", counter.fetch)
    assert counter.calls == 1
    clock.t = 4.1
    await cache.get("k", counter.fetch)
    assert counter.calls == 2


@pytest.mark.asyncio
async def test_none_results_are_cached() -> None:
    cache: AsyncTTLCache[str, object] = AsyncTTLCache(ttl_seconds=10.0)
    counter = _Counter(value=None)
    assert await cache.get("k", counter.fetch) is None
    assert await cache.get("k", counter.fetch) is None
    assert counter.calls == 1


@pytest.mark.asyncio
async def test_keys_are_isolated() -> None:
    cache: AsyncTTLCache[str, object] = AsyncTTLCache(ttl_seconds=10.0)
    a, b = _Counter("a"), _Counter("b")
    assert await cache.get("ka", a.fetch) == "a"
    assert await cache.get("kb", b.fetch) == "b"
    assert (a.calls, b.calls) == (1, 1)


@pytest.mark.asyncio
async def test_clear_forces_refetch() -> None:
    cache: AsyncTTLCache[str, object] = AsyncTTLCache(ttl_seconds=10.0)
    counter = _Counter()
    await cache.get("k", counter.fetch)
    cache.clear()
    await cache.get("k", counter.fetch)
    assert counter.calls == 2
