import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import DeviceEvent, DeviceEventType


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


def build_device_crashed_payload(
    *,
    device_id: str,
    device_name: str,
    source: str,
    reason: str,
    will_restart: bool,
    process: str | None = None,
) -> dict[str, Any]:
    """Build the ``device.crashed`` event payload. Keeps the domain shape out of the generic publisher."""
    return {
        "device_id": device_id,
        "device_name": device_name,
        "source": source,
        "reason": reason,
        "will_restart": will_restart,
        "process": process,
    }
