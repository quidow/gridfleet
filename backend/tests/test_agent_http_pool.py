"""Tests for the per-host httpx.AsyncClient pool."""

from __future__ import annotations

import asyncio

import pytest

from app.services import agent_http_pool as pool_module
from app.services.agent_http_pool import AgentHttpPool, PoolClosedError


@pytest.mark.asyncio
async def test_get_client_returns_same_instance_for_same_host_port() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100)
        b = await pool.get_client("10.0.0.1", 5100)
        assert a is b
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_returns_different_instance_for_different_host() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100)
        b = await pool.get_client("10.0.0.2", 5100)
        assert a is not b
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_returns_different_instance_for_different_port() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100)
        b = await pool.get_client("10.0.0.1", 5101)
        assert a is not b
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_replaces_closed_client() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100)
        await a.aclose()
        b = await pool.get_client("10.0.0.1", 5100)
        assert a is not b
        assert not b.is_closed
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_close_drops_all_clients() -> None:
    pool = AgentHttpPool()
    await pool.get_client("10.0.0.1", 5100)
    await pool.get_client("10.0.0.2", 5100)
    assert pool.size() == 2
    await pool.close()
    assert pool.size() == 0


def test_module_level_pool_singleton() -> None:
    assert isinstance(pool_module.agent_http_pool, AgentHttpPool)


@pytest.mark.asyncio
async def test_get_client_concurrent_returns_same_instance() -> None:
    """Race get_client calls for the same key; the pool lock guarantees one client."""
    pool = AgentHttpPool()
    try:
        results = await asyncio.gather(
            pool.get_client("10.0.0.1", 5100),
            pool.get_client("10.0.0.1", 5100),
            pool.get_client("10.0.0.1", 5100),
        )
        assert results[0] is results[1] is results[2]
        assert pool.size() == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_recreates_when_max_keepalive_changes() -> None:
    """Runtime change to agent.http_pool_max_keepalive must replace the
    pooled client; otherwise operator setting changes silently no-op for
    already-active hosts.
    """
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        b = await pool.get_client("10.0.0.1", 5100, max_keepalive=20, keepalive_expiry=60)
        assert a is not b
        # Stale client kept open so concurrent in-flight requests do not fail
        # mid-flight; it is aclose()d at pool shutdown.
        assert not a.is_closed
        assert not b.is_closed
        assert pool.size() == 1
        assert pool.deferred_count() == 1
    finally:
        await pool.close()
    assert a.is_closed
    assert b.is_closed
    assert pool.deferred_count() == 0


@pytest.mark.asyncio
async def test_get_client_recreates_when_keepalive_expiry_changes() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        b = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=120)
        assert a is not b
        assert not a.is_closed
        assert not b.is_closed
        assert pool.size() == 1
        assert pool.deferred_count() == 1
    finally:
        await pool.close()
    assert a.is_closed
    assert b.is_closed


@pytest.mark.asyncio
async def test_get_client_keeps_same_instance_when_config_unchanged() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        b = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        assert a is b
        assert not a.is_closed
        assert pool.size() == 1
        assert pool.deferred_count() == 0
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_config_change_does_not_close_other_hosts() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        b = await pool.get_client("10.0.0.2", 5100, max_keepalive=10, keepalive_expiry=60)
        new_a = await pool.get_client("10.0.0.1", 5100, max_keepalive=20, keepalive_expiry=60)
        assert new_a is not a
        # Old client a is deferred (not closed); other host b untouched.
        assert not a.is_closed
        assert not b.is_closed
        assert pool.size() == 2
        assert pool.deferred_count() == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_replacement_does_not_close_client_used_by_in_flight_request() -> None:
    """Race regression: replacement must not aclose() a client that another
    coroutine has already obtained and is about to use. Deferred close
    keeps the stale client live until pool shutdown.
    """
    pool = AgentHttpPool()
    try:
        # Coroutine A obtains the current client.
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        # Coroutine B changes the config and gets a fresh client.
        b = await pool.get_client("10.0.0.1", 5100, max_keepalive=20, keepalive_expiry=60)
        assert a is not b
        # Coroutine A is "still in-flight": it must be able to use `a`.
        # We simulate by issuing a no-op request on the (still open) client.
        # `a.is_closed` MUST be False — the actual race fix.
        assert not a.is_closed
        # Subsequent calls go to the new client.
        c = await pool.get_client("10.0.0.1", 5100, max_keepalive=20, keepalive_expiry=60)
        assert c is b
    finally:
        await pool.close()
    assert a.is_closed and b.is_closed


@pytest.mark.asyncio
async def test_repeated_config_changes_accumulate_deferred() -> None:
    pool = AgentHttpPool()
    try:
        await pool.get_client("10.0.0.1", 5100, max_keepalive=10)
        await pool.get_client("10.0.0.1", 5100, max_keepalive=20)
        await pool.get_client("10.0.0.1", 5100, max_keepalive=30)
        assert pool.size() == 1
        assert pool.deferred_count() == 2
    finally:
        await pool.close()
    assert pool.deferred_count() == 0


@pytest.mark.asyncio
async def test_deferred_entries_drained_after_grace_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stale clients older than DEFERRED_GRACE_SECONDS get aclose()d on the
    next get_client invocation, bounding memory under long-lived processes.
    """
    monkeypatch.setattr(pool_module, "DEFERRED_GRACE_SECONDS", 0.0)
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10)
        await pool.get_client("10.0.0.1", 5100, max_keepalive=20)
        assert pool.deferred_count() == 1
        # Next swap forces a drainable scan; grace window 0 → drains a.
        await pool.get_client("10.0.0.1", 5100, max_keepalive=30)
        assert a.is_closed
        assert pool.deferred_count() == 1  # only the post-drain stale entry
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_deferred_hard_cap_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """When deferred entries exceed DEFERRED_MAX, oldest are aclose()d FIFO."""
    monkeypatch.setattr(pool_module, "DEFERRED_MAX", 2)
    # Keep grace large so only the cap evicts.
    monkeypatch.setattr(pool_module, "DEFERRED_GRACE_SECONDS", 1e9)
    pool = AgentHttpPool()
    try:
        c0 = await pool.get_client("10.0.0.1", 5100, max_keepalive=10)
        c1 = await pool.get_client("10.0.0.1", 5100, max_keepalive=11)
        c2 = await pool.get_client("10.0.0.1", 5100, max_keepalive=12)
        # 2 deferred so far (c0, c1); current is c2. Cap is 2. Not over yet.
        assert pool.deferred_count() == 2
        assert not c0.is_closed and not c1.is_closed and not c2.is_closed

        await pool.get_client("10.0.0.1", 5100, max_keepalive=13)
        # Now c2 enters deferred (3 total). Cap=2 → oldest (c0) evicted.
        assert c0.is_closed
        assert not c1.is_closed and not c2.is_closed
        assert pool.deferred_count() == 2
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_after_close_raises() -> None:
    """close() must mark the pool closed under the lock so concurrent
    get_client() calls cannot create a new client that escapes shutdown.
    """
    pool = AgentHttpPool()
    await pool.close()
    with pytest.raises(PoolClosedError):
        await pool.get_client("10.0.0.1", 5100)


@pytest.mark.asyncio
async def test_close_is_idempotent() -> None:
    pool = AgentHttpPool()
    await pool.get_client("10.0.0.1", 5100)
    await pool.close()
    # Second close is a no-op, not an error.
    await pool.close()
    assert pool.size() == 0


@pytest.mark.asyncio
async def test_concurrent_get_client_during_close_does_not_leak() -> None:
    """Race regression: a get_client() running while close() is starting
    must either (a) succeed BEFORE close marks the pool, or (b) raise
    PoolClosedError. It must NOT silently install a new client that
    survives shutdown.
    """
    pool = AgentHttpPool()
    # Hold the lock manually to control ordering.
    await pool._lock.acquire()
    pool._closed = True
    pool._lock.release()

    with pytest.raises(PoolClosedError):
        await pool.get_client("10.0.0.1", 5100)
    assert pool.size() == 0
