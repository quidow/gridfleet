"""Runs domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.pagination import CursorPage
    from app.devices.models import Device, DeviceHold, DeviceOperationalState
    from app.devices.models.reservation import DeviceReservation
    from app.events.catalog import EventSeverity
    from app.runs.models import RunState, TestRun
    from app.runs.schemas import ReservedDeviceInfo, RunCreate, SessionCounts


@runtime_checkable
class RunAllocatorProtocol(Protocol):
    async def create_run(self, db: AsyncSession, data: RunCreate) -> tuple[TestRun, list[ReservedDeviceInfo]]: ...


@runtime_checkable
class RunReleaseProtocol(Protocol):
    async def release_devices(
        self, db: AsyncSession, run: TestRun, *, commit: bool = True, terminate_grid_sessions: bool = False
    ) -> list[uuid.UUID]: ...
    async def clear_desired_grid_run_id_for_run(
        self, db: AsyncSession, *, run: TestRun, caller: str, actor: str | None = None, reason: str | None = None
    ) -> None: ...
    async def complete_deferred_stops_post_commit(self, db: AsyncSession, device_ids: list[uuid.UUID]) -> None: ...


@runtime_checkable
class RunLifecycleProtocol(Protocol):
    async def signal_ready(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun: ...
    async def signal_active(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun: ...
    async def heartbeat(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun: ...
    async def complete_run(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun: ...
    async def cancel_run(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun: ...
    async def force_release(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun: ...
    async def expire_run(self, db: AsyncSession, run: TestRun, reason: str) -> None: ...


@runtime_checkable
class RunFailureProtocol(Protocol):
    async def report_preparation_failure(
        self,
        db: AsyncSession,
        run_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        message: str,
        source: str = "ci_preparation",
    ) -> TestRun: ...
    async def cooldown_device(
        self,
        db: AsyncSession,
        run_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        reason: str,
        ttl_seconds: int,
    ) -> tuple[datetime | None, int, bool, int]: ...


@runtime_checkable
class RunQueryProtocol(Protocol):
    async def list_runs(
        self,
        db: AsyncSession,
        state: RunState | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> tuple[list[TestRun], int]: ...
    async def list_runs_cursor(
        self,
        db: AsyncSession,
        state: RunState | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        limit: int = 50,
        cursor: str | None = None,
        direction: str = "older",
    ) -> CursorPage[TestRun]: ...
    async def fetch_session_counts(
        self, db: AsyncSession, run_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, SessionCounts]: ...
    async def hydrate_reserved_device_info(
        self, db: AsyncSession, info: ReservedDeviceInfo, device: Device, *, includes: set[str]
    ) -> None: ...
    async def hydrate_reserved_device_infos(
        self, db: AsyncSession, pairs: list[tuple[ReservedDeviceInfo, Device]], *, includes: set[str]
    ) -> None: ...


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
    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> None: ...


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

    async def record_ci_preparation_failed(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reason: str,
        source: str,
    ) -> None: ...
