"""Run domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.grid.protocols import GridServiceProtocol


@dataclass(frozen=True, slots=True)
class RunServices:
    publisher: EventPublisher
    settings: SettingsReader
    grid: GridServiceProtocol
    session_factory: async_sessionmaker[AsyncSession]
