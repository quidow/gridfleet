"""Event domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.events.event_bus import EventBus
    from app.events.protocols import EventPublisher


@dataclass(frozen=True, slots=True)
class EventServices:
    publisher: EventPublisher
    subscriber: EventBus
    reader: EventBus
    session_factory: async_sessionmaker[AsyncSession]
    engine: AsyncEngine
