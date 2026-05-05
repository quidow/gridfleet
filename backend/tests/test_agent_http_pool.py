"""Tests for the per-host httpx.AsyncClient pool."""

from __future__ import annotations

import asyncio

import pytest

from app.services import agent_http_pool as pool_module
from app.services.agent_http_pool import AgentHttpPool


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
        assert a.is_closed
        assert not b.is_closed
        assert pool.size() == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_recreates_when_keepalive_expiry_changes() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        b = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=120)
        assert a is not b
        assert a.is_closed
        assert not b.is_closed
        assert pool.size() == 1
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_get_client_keeps_same_instance_when_config_unchanged() -> None:
    pool = AgentHttpPool()
    try:
        a = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        b = await pool.get_client("10.0.0.1", 5100, max_keepalive=10, keepalive_expiry=60)
        assert a is b
        assert not a.is_closed
        assert pool.size() == 1
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
        assert a.is_closed
        assert not b.is_closed
        assert pool.size() == 2
    finally:
        await pool.close()
