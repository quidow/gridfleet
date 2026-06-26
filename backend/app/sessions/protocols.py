"""Session domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.pagination import CursorPage
    from app.devices.models import Device
    from app.sessions.filters import SessionFilters
    from app.sessions.models import Session, SessionStatus


class SessionCrudProtocol(Protocol):
    # --- reads ---
    async def list_sessions(
        self,
        db: AsyncSession,
        *,
        filters: SessionFilters,
        limit: int = 50,
        offset: int = 0,
        sort_by: str = "started_at",
        sort_dir: str = "desc",
        include_probes: bool = False,
    ) -> tuple[list[Session], int]: ...

    async def list_sessions_cursor(
        self,
        db: AsyncSession,
        *,
        filters: SessionFilters,
        limit: int = 50,
        cursor: str | None = None,
        direction: str = "older",
        include_probes: bool = False,
    ) -> CursorPage[Session]: ...

    async def get_session(self, db: AsyncSession, session_id: str) -> Session | None: ...

    async def get_device_session_outcome_heatmap_rows(
        self, db: AsyncSession, device_id: uuid.UUID, *, days: int
    ) -> list[tuple[datetime, SessionStatus]]: ...

    # --- writes ---
    async def update_session_status(
        self, db: AsyncSession, session_id: str, status: SessionStatus
    ) -> Session | None: ...


class DeviceSessionLifecycle(Protocol):
    async def handle_session_finished(self, db: AsyncSession, device: Device) -> object: ...
    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> object: ...


class HealthFailureHandler(Protocol):
    async def __call__(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> object: ...


class DeviceCapabilityReader(Protocol):
    async def get_device_capabilities(
        self, db: AsyncSession, device: Device, *, active_connection_target: str | None = ...
    ) -> dict[str, Any]: ...


class DeviceSessionViabilityWriter(Protocol):
    async def update_session_viability(
        self, db: AsyncSession, device: Device, *, status: str | None, error: str | None
    ) -> None: ...
