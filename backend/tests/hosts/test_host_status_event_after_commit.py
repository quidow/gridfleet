"""Contract tests for heartbeat host status events."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: TC002

from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.heartbeat_outcomes import ClientMode, HeartbeatOutcome, HeartbeatPingResult
from tests.fakes import FakeSettingsReader
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

    monkeypatch.setattr("app.appium_nodes.services.heartbeat._ping_agent", AsyncMock(return_value=_DEAD_RESULT))
    monkeypatch.setattr("app.appium_nodes.services.heartbeat.assert_current_leader", AsyncMock())
    from tests.helpers import test_event_bus as event_bus

    # The resume guard uses _last_cycle_monotonic to detect a paused
    # backend (>= max_missed * interval gap between cycles). On slow CI runners the
    # gap between the last unrelated test that called _check_hosts and this one can
    # exceed the threshold, causing the guard to swallow the offline cascade we are
    # asserting. Reset to None so the guard treats this call as the first cycle.
    svc = HeartbeatService(
        publisher=event_bus,
        settings=FakeSettingsReader(
            {
                "general.max_missed_heartbeats": 1,
                "general.heartbeat_interval_sec": 60,
            }
        ),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=db_session_maker,
    )
    svc._last_cycle_monotonic = None

    await svc._check_hosts(db_session)
    await settle_after_commit_tasks()

    types_in_order = [n for n, _ in event_bus_capture]
    assert "host.status_changed" in types_in_order
    assert "host.heartbeat_lost" in types_in_order
    avail = [p for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert len(avail) == len(devices)
    assert all(p["new_operational_state"] == "offline" for p in avail)
