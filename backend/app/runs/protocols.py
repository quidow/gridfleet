"""Runs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.devices.models.reservation import DeviceReservation
    from app.events.protocols import EventPublisher
    from app.runs.models import TestRun


@runtime_checkable
class RunReleaseProtocol(Protocol):
    async def release_devices(
        self, db: AsyncSession, run: TestRun, *, commit: bool = True, terminate_grid_sessions: bool = False
    ) -> list[uuid.UUID]: ...
    async def clear_desired_grid_run_id_for_run(
        self,
        db: AsyncSession,
        *,
        run: TestRun,
        caller: str,
        actor: str | None = None,
        reason: str | None = None,
        stop_device_ids: set[uuid.UUID] | None = None,
    ) -> None: ...
    async def complete_deferred_stops_post_commit(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> None: ...
    async def terminate_run_sessions_and_probe_survivors(self, db: AsyncSession, run: TestRun) -> set[uuid.UUID]: ...


@runtime_checkable
class MaintenanceWriter(Protocol):
    async def enter_maintenance(
        self,
        db: AsyncSession,
        device: Device,
        *,
        commit: bool = ...,
        allow_reserved: bool = ...,
        maintenance_reason: str = ...,
    ) -> Device: ...


@runtime_checkable
class DeviceDeferredStop(Protocol):
    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> object: ...


@runtime_checkable
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


@runtime_checkable
class RunReservationProtocol(Protocol):
    async def exclude_device_from_run(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        reason: str,
        commit: bool = ...,
    ) -> TestRun | None: ...
    async def restore_device_to_run(
        self, db: AsyncSession, device_id: uuid.UUID, *, commit: bool = ...
    ) -> TestRun | None: ...
    async def release_device_from_run(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        reason: str,
        publisher: EventPublisher,
        commit: bool = ...,
    ) -> TestRun | None: ...
