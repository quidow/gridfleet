import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device_event import DeviceEvent, DeviceEventType


async def record_event(
    db: AsyncSession,
    device_id: uuid.UUID,
    event_type: DeviceEventType,
    details: dict[str, Any] | None = None,
) -> DeviceEvent:
    """Persist a device incident event for analytics. Does not commit — caller controls the transaction."""
    event = DeviceEvent(device_id=device_id, event_type=event_type, details=details)
    db.add(event)
    return event
