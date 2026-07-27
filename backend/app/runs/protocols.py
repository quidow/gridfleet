"""Runs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.locking import LockedDevice
    from app.devices.models import Device
    from app.devices.models.reservation import DeviceReservation
    from app.runs.models import TestRun


class MaintenanceWriter(Protocol):
    """The narrow maintenance view the run-failure escalation needs.

    Only the locked helper: the escalation already holds the Device aggregate
    lock inside its own transaction, so it must never re-acquire it or open a
    second boundary.
    """

    async def enter_maintenance_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        *,
        allow_reserved: bool = ...,
        maintenance_reason: str = ...,
    ) -> None: ...


class DeviceDeferredStop(Protocol):
    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> object: ...


class DeviceLifecycleFailureWriter(Protocol):
    async def exclude_run_if_needed(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reason: str,
        source: str,
    ) -> tuple[TestRun | None, DeviceReservation | None]: ...

    async def record_run_escalation_failure(
        self, db: AsyncSession, device: Device, *, reason: str, source: str, action: str
    ) -> None: ...
