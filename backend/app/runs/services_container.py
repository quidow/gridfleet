"""Run domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.runs.protocols import (
        RunAllocatorProtocol,
        RunFailureProtocol,
        RunLifecycleProtocol,
        RunQueryProtocol,
        RunReleaseProtocol,
        RunReservationProtocol,
    )


@dataclass(frozen=True, slots=True)
class RunServices:
    allocator: RunAllocatorProtocol
    lifecycle: RunLifecycleProtocol
    release: RunReleaseProtocol
    failure: RunFailureProtocol
    reservation: RunReservationProtocol
    query: RunQueryProtocol
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
