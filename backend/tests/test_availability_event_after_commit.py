"""Contract test for after-commit dispatch of device operational-state events.

Codeant flagged that pre-fix `event_bus.publish` ran inside the state writer
with its own session and committed independently of the outer transaction. A rollback on
the caller's session left the SystemEvent row + SSE/webhook delivery in place. Helper
must now queue events on the session and dispatch only after the outer commit succeeds.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TC002

from app.models.device import DeviceOperationalState
from app.services import device_locking
from app.services.device_state import set_operational_state
from tests.helpers import seed_host_and_device, settle_after_commit_tasks

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_event_dispatches_after_commit(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="after-commit-1")
    event_bus_capture.clear()
    locked = await device_locking.lock_device(db_session, device.id)
    await set_operational_state(locked, DeviceOperationalState.offline, reason="under-test")
    # Pre-commit: nothing dispatched yet.
    await settle_after_commit_tasks()
    assert event_bus_capture == [], f"Helper must not dispatch before commit; got {event_bus_capture}"

    await db_session.commit()
    # The after_commit hook schedules a task; let it run.
    await settle_after_commit_tasks()

    avail = [(n, p) for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert len(avail) == 1, f"Expected one event after commit; got {avail}"
    assert avail[0][1]["new_operational_state"] == "offline"
    assert avail[0][1]["reason"] == "under-test"


async def test_event_dropped_on_rollback(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="rollback-1")
    event_bus_capture.clear()
    locked = await device_locking.lock_device(db_session, device.id)
    await set_operational_state(locked, DeviceOperationalState.offline, reason="under-test-rollback")

    await db_session.rollback()
    await settle_after_commit_tasks()

    avail = [(n, p) for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert avail == [], f"Rollback must drop queued events; got {avail}"


async def test_multiple_events_dispatch_in_queue_order(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """Events queued on one session dispatch in FIFO order after commit."""
    _, d1 = await seed_host_and_device(db_session, identity="multi-a")
    _, d2 = await seed_host_and_device(db_session, identity="multi-b")
    event_bus_capture.clear()
    for d in (d1, d2):
        locked = await device_locking.lock_device(db_session, d.id)
        await set_operational_state(locked, DeviceOperationalState.offline, reason="batch")

    await db_session.commit()
    await settle_after_commit_tasks()

    avail = [p for n, p in event_bus_capture if n == "device.operational_state_changed"]
    assert [p["device_name"] for p in avail] == ["Device multi-a", "Device multi-b"], (
        f"Events must dispatch in queue order; got {[p['device_name'] for p in avail]}"
    )
    assert all(p["new_operational_state"] == "offline" for p in avail)
