"""Contract tests for node.state_changed after-commit dispatch."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.models.device import DeviceAvailabilityStatus
from app.services import device_locking
from app.services.node_manager_state import mark_node_started, mark_node_stopped
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_mark_node_started_queues_state_changed_after_availability(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(
        db_session,
        identity="node-start-1",
        availability=DeviceAvailabilityStatus.offline,
    )
    event_bus_capture.clear()

    locked = await device_locking.lock_device(db_session, device.id)
    await mark_node_started(db_session, locked, port=4730, pid=42)
    await settle_after_commit_tasks()

    types_in_order = [name for name, _ in event_bus_capture]
    assert "device.availability_changed" in types_in_order
    assert "node.state_changed" in types_in_order
    avail_idx = types_in_order.index("device.availability_changed")
    node_idx = types_in_order.index("node.state_changed")
    assert avail_idx < node_idx, f"FIFO order: availability must precede node.state_changed; got {types_in_order}"

    node_payload = next(p for n, p in event_bus_capture if n == "node.state_changed")
    assert node_payload["new_state"] == "running"
    assert node_payload["port"] == 4730


async def test_mark_node_stopped_queues_state_changed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="node-stop-1")
    event_bus_capture.clear()

    locked = await device_locking.lock_device(db_session, device.id)
    await mark_node_started(db_session, locked, port=4731, pid=43)
    event_bus_capture.clear()

    locked = await device_locking.lock_device(db_session, device.id)
    await mark_node_stopped(db_session, locked)
    await settle_after_commit_tasks()

    node_events = [p for n, p in event_bus_capture if n == "node.state_changed"]
    assert len(node_events) == 1
    assert node_events[0]["new_state"] == "stopped"
