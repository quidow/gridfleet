"""Contract tests for device.crashed event queueing."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from app.devices.services.event import build_device_crashed_payload
from app.lifecycle.services.incidents import LifecycleIncidentService
from tests.fakes import build_review_service
from tests.helpers import seed_host_and_device, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


async def test_device_crashed_dispatches_after_commit(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    _, device = await seed_host_and_device(db_session, identity="crash-1")
    event_bus_capture.clear()

    event_bus.queue_for_session(
        db_session,
        "device.crashed",
        build_device_crashed_payload(
            device_id=str(device.id),
            device_name=device.name,
            source="appium_crash",
            reason="exit code 137",
            will_restart=True,
            process="appium",
        ),
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

    event_bus.queue_for_session(
        db_session,
        "device.crashed",
        build_device_crashed_payload(
            device_id=str(device.id),
            device_name=device.name,
            source="connectivity_lost",
            reason="adb disconnect",
            will_restart=False,
        ),
    )
    await db_session.rollback()
    await settle_after_commit_tasks()

    assert [n for n, _ in event_bus_capture if n == "device.crashed"] == []


async def test_handle_node_crash_queues_device_crashed(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    from app.devices import locking as device_locking
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.runs.service_reservation import RunReservationService

    _, device = await seed_host_and_device(db_session, identity="lifecycle-crash-1")
    event_bus_capture.clear()
    locked = await device_locking.lock_device(db_session, device.id)

    await LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    ).handle_node_crash(
        db_session,
        locked,
        source="connectivity_lost",
        reason="ADB disconnect",
    )
    await db_session.commit()
    await settle_after_commit_tasks()

    crashed = [p for n, p in event_bus_capture if n == "device.crashed"]
    assert len(crashed) == 1
    assert crashed[0]["source"] == "connectivity_lost"
    assert crashed[0]["reason"] == "ADB disconnect"
    assert crashed[0]["process"] is None


async def test_handle_node_crash_skips_crashed_event_when_already_offline(
    db_session: AsyncSession,
    event_bus_capture: list[tuple[str, dict[str, Any]]],
) -> None:
    """A device that is already offline cannot crash again. handle_node_crash
    must skip the device.crashed event and node_crash DB event when the device
    is already in offline state."""
    from app.devices import locking as device_locking
    from app.devices.models import DeviceEvent, DeviceEventType, DeviceOperationalState
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.runs.service_reservation import RunReservationService

    _, device = await seed_host_and_device(
        db_session, identity="already-offline-crash", operational_state=DeviceOperationalState.offline
    )
    event_bus_capture.clear()
    locked = await device_locking.lock_device(db_session, device.id)

    await LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=RunReservationService(review=build_review_service()),
        incidents=LifecycleIncidentService(),
    ).handle_node_crash(
        db_session,
        locked,
        source="session_viability",
        reason="Recovery probe failed",
    )
    await db_session.commit()
    await settle_after_commit_tasks()

    # device.crashed must NOT fire for an already-offline device
    crashed = [p for n, p in event_bus_capture if n == "device.crashed"]
    assert crashed == [], "device.crashed must not fire for already-offline device"

    # node_crash DB event must NOT be recorded
    from sqlalchemy import select

    result = await db_session.execute(select(DeviceEvent.event_type).where(DeviceEvent.device_id == device.id))
    event_types = list(result.scalars().all())
    assert DeviceEventType.node_crash not in event_types, (
        "node_crash event must not be recorded for already-offline device"
    )
