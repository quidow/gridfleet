"""Event domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.events.event_bus import EventBus
    from app.events.protocols import EventReader, EventSubscriber


@dataclass(frozen=True, slots=True)
class EventServices:
    publisher: EventBus
    subscriber: EventSubscriber
    reader: EventReader
    session_factory: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
