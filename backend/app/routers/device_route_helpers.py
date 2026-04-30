import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device
from app.services import device_service


async def get_device_or_404(device_id: uuid.UUID, db: AsyncSession) -> Device:
    device = await device_service.get_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return device
