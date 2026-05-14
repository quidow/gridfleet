"""DeviceEvent + SystemEvent factories."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.devices.models import DeviceEvent, DeviceEventType
from app.events.models import SystemEvent

if TYPE_CHECKING:
    from datetime import datetime

    from app.seeding.context import SeedContext


def make_device_event(
    ctx: SeedContext,
    *,
    device_id: uuid.UUID,
    event_type: DeviceEventType,
    created_at: datetime,
    details: dict[str, Any] | None = None,
) -> DeviceEvent:
    return DeviceEvent(
        device_id=device_id,
        event_type=event_type,
        details=details,
        created_at=created_at,
    )


def make_system_event(
    ctx: SeedContext,
    *,
    event_type: str,
    data: dict[str, Any],
    created_at: datetime,
) -> SystemEvent:
    return SystemEvent(
        event_id=str(uuid.uuid4()),
        type=event_type,
        data=data,
        created_at=created_at,
    )
