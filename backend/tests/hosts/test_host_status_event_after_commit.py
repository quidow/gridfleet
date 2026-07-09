"""Contract tests for heartbeat host status events."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from app.appium_nodes.services.heartbeat import HeartbeatService
from app.core.timeutil import now_utc
from tests.fakes import FakeSettingsReader
from tests.helpers import run_one_heartbeat_cycle, seed_host_with_devices, settle_after_commit_tasks

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_host_offline_cascade_queues_all_events(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    host, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="cascade")
    # Offline now derives from status-push recency: no push within the offline window.
    host.last_heartbeat = now_utc() - timedelta(minutes=10)
    await db_session.commit()
    event_bus_capture.clear()

    from tests.helpers import test_event_bus as event_bus

    svc = HeartbeatService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        pool=Mock(),
        circuit_breaker=Mock(),
        session_factory=db_session_maker,
    )
    # A recent prior-cycle marker makes this sweep's begin_cycle() unguarded (a
    # small monotonic gap), so the stale host flips offline instead of being
    # swallowed as a first-cycle-after-boot resume.
    svc._last_cycle_monotonic = time.monotonic()

    await run_one_heartbeat_cycle(db_session, svc)
    await settle_after_commit_tasks()

    types_in_order = [n for n, _ in event_bus_capture]
    assert "host.status_changed" in types_in_order
    assert "host.heartbeat_lost" in types_in_order
    avail = [p for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert len(avail) == len(devices)
    assert all(p["new_operational_state"] == "offline" for p in avail)
