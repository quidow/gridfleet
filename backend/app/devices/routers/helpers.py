from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound

from app.devices import locking as device_locking

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.protocols import DeviceCrudProtocol


async def get_device_or_404(device_id: uuid.UUID, db: AsyncSession, crud: DeviceCrudProtocol) -> Device:
    device = await crud.get_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


async def get_device_for_update_or_404(device_id: uuid.UUID, db: AsyncSession) -> Device:
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="Device not found") from exc
