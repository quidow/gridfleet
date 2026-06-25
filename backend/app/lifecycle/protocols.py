"""Lifecycle-domain offered protocols."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device, DeviceEvent, DeviceEventType
    from app.lifecycle.services.incidents import LifecycleIncidentDetails


@runtime_checkable
class LifecycleIncidentRecorder(Protocol):
    async def record_lifecycle_incident(
        self,
        db: AsyncSession,
        device: Device,
        event_type: DeviceEventType,
        incident: LifecycleIncidentDetails,
    ) -> DeviceEvent: ...
