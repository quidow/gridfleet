import uuid

from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services import service as device_service


async def get_device_or_404(device_id: uuid.UUID, db: AsyncSession) -> Device:
    device = await device_service.get_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device


async def get_device_for_update_or_404(device_id: uuid.UUID, db: AsyncSession) -> Device:
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound as exc:
        raise HTTPException(status_code=404, detail="Device not found") from exc
