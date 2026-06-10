from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.http_errors import convert_not_found, found_or_404
from app.devices import locking as device_locking

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.protocols import DeviceCrudProtocol


async def get_device_or_404(device_id: uuid.UUID, db: AsyncSession, crud: DeviceCrudProtocol) -> Device:
    return found_or_404(await crud.get_device(db, device_id), "Device not found")


async def get_device_for_update_or_404(device_id: uuid.UUID, db: AsyncSession) -> Device:
    with convert_not_found("Device not found"):
        return await device_locking.lock_device(db, device_id)
