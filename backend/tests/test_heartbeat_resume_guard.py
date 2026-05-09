from __future__ import annotations

import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

import app.services.heartbeat as hb
from app.services.heartbeat_outcomes import (
    ClientMode,
    HeartbeatOutcome,
    HeartbeatPingResult,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from app.models.host import Host


@pytest.fixture(autouse=True)
def _skip_leader_fencing() -> Iterator[None]:
    """No-op assert_current_leader so tests don't need a real leader advisory lock."""
    with patch("app.services.heartbeat.assert_current_leader"):
        yield


@pytest.fixture(autouse=True)
def _reset_last_cycle_monotonic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hb, "_LAST_CYCLE_MONOTONIC", None)


def test_resume_guard_helper() -> None:
    assert (
        hb._resume_guard_active(
            last_cycle_monotonic=100.0,
            now_monotonic=350.0,
            interval_sec=15.0,
            max_missed=3,
        )
        is True
    )
    assert (
        hb._resume_guard_active(
            last_cycle_monotonic=300.0,
            now_monotonic=320.0,
            interval_sec=15.0,
            max_missed=3,
        )
        is False
    )
    assert (
        hb._resume_guard_active(
            last_cycle_monotonic=None,
            now_monotonic=100.0,
            interval_sec=15.0,
            max_missed=3,
        )
        is False
    )
    # Equality boundary: gap == interval * max_missed → guard NOT active (genuine miss).
    assert (
        hb._resume_guard_active(
            last_cycle_monotonic=300.0,
            now_monotonic=345.0,
            interval_sec=15.0,
            max_missed=3,
        )
        is False
    )


def _ok() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload={"status": "ok"},
        duration_ms=10,
        client_mode=ClientMode.pooled,
        http_status=200,
        error_category=None,
    )


def _timeout() -> HeartbeatPingResult:
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.timeout,
        payload=None,
        duration_ms=4_000,
        client_mode=ClientMode.pooled,
        http_status=None,
        error_category="ReadTimeout",
    )


@pytest.mark.asyncio
async def test_long_gap_then_recovery_does_not_emit_offline(
    monkeypatch: pytest.MonkeyPatch,
    db_session: object,
    db_session_maker: object,
    db_host: Host,
    event_bus_capture: list[tuple[str, dict[str, object]]],
) -> None:
    """Set _LAST_CYCLE_MONOTONIC to (now - 10*interval). Cycle 1: timeout; guard active.
    Cycle 2: success. No host.status_changed online->offline event must appear."""
    from sqlalchemy.ext.asyncio import AsyncSession

    assert isinstance(db_session, AsyncSession)

    # Pre-position the cycle counter as if we paused for 10 intervals (10 * 15 = 150s gap).
    monkeypatch.setattr(hb, "_LAST_CYCLE_MONOTONIC", time.monotonic() - 10 * 15.0)

    # Redirect per-host sessions to the test schema engine.
    with patch("app.services.heartbeat.async_session", db_session_maker):
        # Cycle 1: agent times out but guard is active — should NOT mark host offline.
        with patch("app.services.heartbeat._ping_agent", new=AsyncMock(return_value=_timeout())):
            await hb._check_hosts(db_session)

        # Cycle 2: agent comes back online.
        with patch("app.services.heartbeat._ping_agent", new=AsyncMock(return_value=_ok())):
            await hb._check_hosts(db_session)

    # Yield to the event loop so any after-commit publish tasks can run.
    import asyncio

    await asyncio.sleep(0)

    offline_events = [
        (name, payload)
        for name, payload in event_bus_capture
        if name == "host.status_changed" and payload.get("new_status") == "offline"
    ]
    assert offline_events == [], f"Unexpected offline events: {offline_events}"
