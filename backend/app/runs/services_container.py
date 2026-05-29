"""Run domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.grid.protocols import GridServiceProtocol
    from app.runs.protocols import (
        RunAllocatorProtocol,
        RunFailureProtocol,
        RunLifecycleProtocol,
        RunQueryProtocol,
        RunReleaseProtocol,
    )


@dataclass(frozen=True, slots=True)
class RunServices:
    allocator: RunAllocatorProtocol
    lifecycle: RunLifecycleProtocol
    release: RunReleaseProtocol
    failure: RunFailureProtocol
    query: RunQueryProtocol
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
    # Kept temporarily so the reaper (still free-fn based) can access them.
    # Dropped in Task 8 once the reaper delegates to services.lifecycle.
    publisher: EventPublisher
    grid: GridServiceProtocol
