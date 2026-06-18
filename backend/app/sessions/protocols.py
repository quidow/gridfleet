"""Session domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.pagination import CursorPage
    from app.devices.models import Device
    from app.sessions.models import Session, SessionStatus
    from app.sessions.viability_types import SessionViabilityCheckedBy


@runtime_checkable
class SessionCrudProtocol(Protocol):
    # --- reads ---
    async def list_sessions(
        self,
        db: AsyncSession,
        device_id: uuid.UUID | None = None,
        status: SessionStatus | None = None,
        pack_id: str | None = None,
        platform_id: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "started_at",
        sort_dir: str = "desc",
        include_probes: bool = False,
        active: bool = False,
    ) -> tuple[list[Session], int]: ...

    async def list_sessions_cursor(
        self,
        db: AsyncSession,
        device_id: uuid.UUID | None = None,
        status: SessionStatus | None = None,
        pack_id: str | None = None,
        platform_id: str | None = None,
        started_after: datetime | None = None,
        started_before: datetime | None = None,
        run_id: uuid.UUID | None = None,
        limit: int = 50,
        cursor: str | None = None,
        direction: str = "older",
        include_probes: bool = False,
        active: bool = False,
    ) -> CursorPage[Session]: ...

    async def get_session(self, db: AsyncSession, session_id: str) -> Session | None: ...

    async def get_device_session_outcome_heatmap_rows(
        self, db: AsyncSession, device_id: uuid.UUID, *, days: int
    ) -> list[tuple[datetime, SessionStatus]]: ...

    # --- writes ---
    async def update_session_status(
        self, db: AsyncSession, session_id: str, status: SessionStatus
    ) -> Session | None: ...


@runtime_checkable
class SessionSyncProtocol(Protocol):
    async def sync(self, db: AsyncSession) -> None: ...
    def wake(self) -> None: ...
    async def wait_for_wake(self, timeout: float) -> bool: ...


@runtime_checkable
class DeviceSessionLifecycle(Protocol):
    async def handle_session_finished(self, db: AsyncSession, device: Device) -> object: ...
    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> object: ...


@runtime_checkable
class HealthFailureHandler(Protocol):
    async def __call__(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> object: ...


@runtime_checkable
class SessionViabilityProtocol(Protocol):
    async def get_session_viability(self, db: AsyncSession, device: Device) -> dict[str, Any] | None: ...
    async def run_session_viability_probe(
        self, db: AsyncSession, device: Device, *, checked_by: SessionViabilityCheckedBy
    ) -> dict[str, Any]: ...
    async def check_due_devices(self, db: AsyncSession) -> None: ...
    def configure_health_failure_handler(self, handler: HealthFailureHandler | None) -> None: ...


@runtime_checkable
class DeviceCapabilityReader(Protocol):
    async def get_device_capabilities(
        self, db: AsyncSession, device: Device, *, active_connection_target: str | None = ...
    ) -> dict[str, Any]: ...


@runtime_checkable
class DeviceSessionViabilityWriter(Protocol):
    async def update_session_viability(
        self, db: AsyncSession, device: Device, *, status: str | None, error: str | None
    ) -> None: ...
