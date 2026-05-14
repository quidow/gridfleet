"""Contract tests for heartbeat host status events."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: TC002

from app.services.heartbeat import _check_hosts
from app.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from tests.helpers import seed_host_with_devices, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

_DEAD_RESULT = HeartbeatPingResult(
    outcome=HeartbeatOutcome.connect_error,
    payload=None,
    duration_ms=0,
    client_mode=ClientMode.pooled,
    http_status=None,
    error_category=None,
)


async def test_host_offline_cascade_queues_all_events(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="cascade")
    event_bus_capture.clear()

    monkeypatch.setattr("app.services.heartbeat._ping_agent", AsyncMock(return_value=_DEAD_RESULT))
    monkeypatch.setattr(
        "app.settings.service.settings_service.get",
        lambda key: 1 if key == "general.max_missed_heartbeats" else 60,
    )
    monkeypatch.setattr("app.services.heartbeat.assert_current_leader", AsyncMock())
    # Redirect per-host sessions to the test schema engine so events are queued
    # on sessions that share the same after-commit event hook configuration.
    monkeypatch.setattr("app.services.heartbeat.async_session", db_session_maker)
    # The resume guard uses a module-level _LAST_CYCLE_MONOTONIC to detect a paused
    # backend (>= max_missed * interval gap between cycles). On slow CI runners the
    # gap between the last unrelated test that called _check_hosts and this one can
    # exceed the threshold, causing the guard to swallow the offline cascade we are
    # asserting. Reset to None so the guard treats this call as the first cycle.
    monkeypatch.setattr("app.services.heartbeat._LAST_CYCLE_MONOTONIC", None)

    await _check_hosts(db_session)
    await settle_after_commit_tasks()

    types_in_order = [n for n, _ in event_bus_capture]
    assert "host.status_changed" in types_in_order
    assert "host.heartbeat_lost" in types_in_order
    avail = [p for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert len(avail) == len(devices)
    assert all(p["new_operational_state"] == "offline" for p in avail)
