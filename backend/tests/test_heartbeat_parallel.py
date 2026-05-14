from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult

if TYPE_CHECKING:
    import contextlib
    from collections.abc import AsyncGenerator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
async def _skip_leader_fencing(
    db_session_maker: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[None]:
    """No-op the leader fence and redirect per-host sessions to the test engine."""
    with (
        patch("app.appium_nodes.services.heartbeat.assert_current_leader"),
        patch("app.appium_nodes.services.heartbeat.async_session", db_session_maker),
    ):
        yield


def _ok() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={"status": "ok"},
        duration_ms=10,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )


def _slow_timeout() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=4_000,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="ReadTimeout",
    )


@pytest.mark.asyncio
async def test_four_slow_hosts_run_in_parallel(
    populated_hosts_4_slow: contextlib.AbstractAsyncContextManager[AsyncSession],
) -> None:
    """4 slow hosts each take 0.5s; sequential >= 2s, parallel (concurrency>=4) ~ 0.5s.
    Bound: < 1.8s leaves CI slack while still proving parallelism."""
    from app.appium_nodes.services.heartbeat import _check_hosts

    async def fake_ping(ip: str, port: int) -> HeartbeatPingResult:
        await asyncio.sleep(0.5)
        return _slow_timeout()

    with patch("app.appium_nodes.services.heartbeat._ping_agent", new=AsyncMock(side_effect=fake_ping)):
        started = time.monotonic()
        async with populated_hosts_4_slow as db:
            await _check_hosts(db)
        elapsed = time.monotonic() - started
    assert elapsed < 1.8, f"Expected parallelization to bring runtime under 1.8s, got {elapsed:.1f}s"


@pytest.mark.asyncio
async def test_one_slow_host_does_not_delay_fast_host_log(
    populated_hosts_one_slow_one_fast: contextlib.AbstractAsyncContextManager[AsyncSession],
) -> None:
    """Verify the fast host's heartbeat_ping log appears BEFORE the slow host's log."""
    import structlog

    from app.appium_nodes.services.heartbeat import _check_hosts

    async def fake_ping(ip: str, port: int) -> HeartbeatPingResult:
        if ip == "1.1.1.1":
            await asyncio.sleep(0.5)
            return _slow_timeout()
        return _ok()

    with (
        structlog.testing.capture_logs() as cap,
        patch("app.appium_nodes.services.heartbeat._ping_agent", new=AsyncMock(side_effect=fake_ping)),
    ):
        async with populated_hosts_one_slow_one_fast as db:
            await _check_hosts(db)

    # Look for heartbeat_ping events; assert fast host's event index < slow host's index.
    events = [e for e in cap if e.get("event") == "heartbeat_ping"]
    fast_index = next(i for i, e in enumerate(events) if e.get("host_ip") == "2.2.2.2")
    slow_index = next(i for i, e in enumerate(events) if e.get("host_ip") == "1.1.1.1")
    assert fast_index < slow_index
