"""Contract tests for device.crashed event queueing."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.services.event_bus import queue_device_crashed_event
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_device_crashed_dispatches_after_commit(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="crash-1")
    event_bus_capture.clear()

    queue_device_crashed_event(
        db_session,
        device_id=str(device.id),
        device_name=device.name,
        source="appium_crash",
        reason="exit code 137",
        will_restart=True,
        process="appium",
    )
    await settle_after_commit_tasks()
    assert event_bus_capture == [], "must not dispatch before commit"

    await db_session.commit()
    await settle_after_commit_tasks()

    crashed = [(n, p) for n, p in event_bus_capture if n == "device.crashed"]
    assert len(crashed) == 1
    assert crashed[0][1] == {
        "device_id": str(device.id),
        "device_name": device.name,
        "source": "appium_crash",
        "reason": "exit code 137",
        "will_restart": True,
        "process": "appium",
    }


async def test_device_crashed_dropped_on_rollback(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="crash-2")
    event_bus_capture.clear()

    queue_device_crashed_event(
        db_session,
        device_id=str(device.id),
        device_name=device.name,
        source="connectivity_lost",
        reason="adb disconnect",
        will_restart=False,
    )
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n == "device.crashed"] == []
