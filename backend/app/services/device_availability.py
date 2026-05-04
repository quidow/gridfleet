from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import inspect as sa_inspect

from app.models.device import Device, DeviceAvailabilityStatus
from app.services.device_readiness import is_ready_for_use_async
from app.services.event_bus import queue_event_for_session

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
        session = device_state.session
        assert session is not None, "set_device_availability_status: device has no session despite persistent==True"
        queue_event_for_session(session, "device.availability_changed", payload)
    return True
