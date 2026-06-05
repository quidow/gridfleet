"""Session domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.protocols import SessionCrudProtocol, SessionSyncProtocol, SessionViabilityProtocol


@dataclass(frozen=True, slots=True)
class SessionServices:
    crud: SessionCrudProtocol
    sync: SessionSyncProtocol
    viability: SessionViabilityProtocol
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
    publisher: EventPublisher
