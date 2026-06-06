"""Grid domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.grid.allocation import AllocationService


@dataclass(frozen=True, slots=True)
class GridServices:
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
    allocation: AllocationService
