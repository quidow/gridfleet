"""Device domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.analytics.models import AnalyticsCapacitySnapshot
    from app.analytics.schemas import FleetCapacityTimeline
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


@runtime_checkable
class FleetCapacityProtocol(Protocol):
    async def get_fleet_capacity_timeline(
        self,
        db: AsyncSession,
        *,
        date_from: datetime,
        date_to: datetime,
        bucket_minutes: int = ...,
    ) -> FleetCapacityTimeline: ...

    async def collect_capacity_snapshot_once(
        self,
        db: AsyncSession,
        *,
        captured_at: datetime | None = ...,
    ) -> AnalyticsCapacitySnapshot | None: ...


@runtime_checkable
class DataCleanupProtocol(Protocol):
    async def cleanup_old_data(self, db: AsyncSession) -> None: ...
