from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import exists, select

from app.devices.models import Device
from app.devices.models.reservation import DeviceReservation

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.expression import ColumnElement


def active_reservation_exists() -> ColumnElement[bool]:
    """Correlated EXISTS clause: an active (``released_at IS NULL``) reservation for the
    current ``Device`` row. Use in ``select(...).where(~active_reservation_exists())`` or as a
    labeled column ``active_reservation_exists().label("is_reserved")``."""
    return exists(
        select(DeviceReservation.id).where(
            DeviceReservation.device_id == Device.id,
            DeviceReservation.released_at.is_(None),
        )
    )


async def device_is_reserved(db: AsyncSession, device_id: UUID) -> bool:
    """True iff the device has an active reservation row."""
    return (
        await db.execute(
            select(DeviceReservation.id)
            .where(DeviceReservation.device_id == device_id, DeviceReservation.released_at.is_(None))
            .limit(1)
        )
    ).first() is not None
