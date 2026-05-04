from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.device import Device, DeviceAvailabilityStatus
from app.models.test_run import RunState
from app.services import run_service
from app.services.device_availability import ready_device_availability_status, set_device_availability_status

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


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
