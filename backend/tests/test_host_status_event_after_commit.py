"""Contract tests for heartbeat host status events."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.services.heartbeat import _check_hosts
from tests.helpers import seed_host_with_devices, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_host_offline_cascade_queues_all_events(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, devices = await seed_host_with_devices(db_session, count=2, identity_prefix="cascade")
    event_bus_capture.clear()

    async def _fake_ping(_ip: str, _port: int) -> None:
        return None

    monkeypatch.setattr("app.services.heartbeat._ping_agent", _fake_ping)
    monkeypatch.setattr(
        "app.services.settings_service.settings_service.get",
        lambda key: 1 if key == "general.max_missed_heartbeats" else 60,
    )
    monkeypatch.setattr("app.services.heartbeat.assert_current_leader", AsyncMock())

    await _check_hosts(db_session)
    await settle_after_commit_tasks()

    types_in_order = [n for n, _ in event_bus_capture]
    assert "host.status_changed" in types_in_order
    assert "host.heartbeat_lost" in types_in_order
    avail = [p for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert len(avail) == len(devices)
    assert all(p["new_operational_state"] == "offline" for p in avail)
