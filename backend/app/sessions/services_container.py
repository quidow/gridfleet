"""Session domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.service import SessionCrudService
    from app.sessions.service_sync import SessionSyncService
    from app.sessions.service_viability import SessionViabilityService


@dataclass(frozen=True, slots=True)
class SessionServices:
    crud: SessionCrudService
    sync: SessionSyncService
    viability: SessionViabilityService
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
    publisher: EventPublisher
