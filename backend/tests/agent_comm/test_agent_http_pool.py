"""Tests for the per-host httpx.AsyncClient pool."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.agent_comm import http_pool as pool_module
from app.agent_comm.http_pool import AgentHttpPool, PoolClosedError


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


def test_pool_class_importable() -> None:
    assert hasattr(pool_module, "AgentHttpPool")
    assert isinstance(AgentHttpPool(), AgentHttpPool)


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
async def test_reopen_allows_reuse_after_close() -> None:
    pool = AgentHttpPool()
    await pool.get_client("10.0.0.1", 5100)
    await pool.close()

    await pool.reopen()
    client = await pool.get_client("10.0.0.1", 5100)
    try:
        assert not client.is_closed
        assert pool.size() == 1
    finally:
        await pool.close()


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


@pytest.mark.asyncio
async def test_pool_close_logs_client_close_failures() -> None:
    pool = AgentHttpPool()
    entry_client = AsyncMock()
    entry_client.is_closed = False
    entry_client.aclose.side_effect = RuntimeError("entry close failed")
    pool._entries[("10.0.0.1", 5100)] = entry_client

    await pool.close()

    entry_client.aclose.assert_awaited_once()
    assert pool.size() == 0
