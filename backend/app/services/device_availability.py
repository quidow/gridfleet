from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect

from app.models.device import Device, DeviceAvailabilityStatus
from app.models.test_run import RunState
from app.services import run_service
from app.services.device_readiness import is_ready_for_use_async
from app.services.event_bus import event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def ready_device_availability_status(db: AsyncSession, device: Device) -> DeviceAvailabilityStatus:
    if await is_ready_for_use_async(db, device):
        return DeviceAvailabilityStatus.available
    return DeviceAvailabilityStatus.offline


async def set_device_availability_status(
    device: Device,
    new_availability_status: DeviceAvailabilityStatus,
    *,
    reason: str | None = None,
    publish_event: bool = True,
) -> bool:
    device_state = sa_inspect(device, raiseerr=False)
    assert device_state is not None and device_state.persistent, (
        "Device must be persistent in a session; callers that write availability "
        "must load it through lock_device in the same transaction"
    )

    old_availability_status = device.availability_status
    if old_availability_status == new_availability_status:
        return False

    device.availability_status = new_availability_status
    if publish_event:
        payload = {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_availability_status": old_availability_status.value,
            "new_availability_status": new_availability_status.value,
        }
        if reason is not None:
            payload["reason"] = reason
        await event_bus.publish("device.availability_changed", payload)
    return True


async def resolve_post_busy_availability_status(db: AsyncSession, device: Device) -> DeviceAvailabilityStatus:
    reserved_run, reserved_entry = await run_service.get_device_reservation_with_entry(db, device.id)
    next_availability_status = await ready_device_availability_status(db, device)
    if (
        reserved_run is not None
        and reserved_run.state not in (RunState.completed, RunState.failed, RunState.expired, RunState.cancelled)
        and not run_service.reservation_entry_is_excluded(reserved_entry)
    ):
        return DeviceAvailabilityStatus.reserved
    return next_availability_status


async def restore_post_busy_availability_status(
    db: AsyncSession,
    device: Device,
    *,
    reason: str | None = None,
    publish_event: bool = True,
) -> DeviceAvailabilityStatus:
    next_availability_status = await resolve_post_busy_availability_status(db, device)
    await set_device_availability_status(device, next_availability_status, reason=reason, publish_event=publish_event)
    return next_availability_status
