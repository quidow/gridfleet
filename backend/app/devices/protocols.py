"""Device domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.devices.models import Device, DeviceHold, DeviceOperationalState
    from app.events.catalog import EventSeverity


@runtime_checkable
class DeviceStateWriter(Protocol):
    async def set_operational_state(
        self,
        device: Device,
        new_state: DeviceOperationalState,
        *,
        reason: str | None = ...,
        publish_event: bool = ...,
        severity: EventSeverity | None = ...,
    ) -> bool: ...

    async def set_hold(
        self,
        device: Device,
        new_hold: DeviceHold | None,
        *,
        reason: str | None = ...,
        publish_event: bool = ...,
        severity: EventSeverity | None = ...,
    ) -> bool: ...
