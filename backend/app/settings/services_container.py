"""Settings domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.settings.service import SettingsService


@dataclass(frozen=True, slots=True)
class SettingsServices:
    service: SettingsService
    session_factory: async_sessionmaker[AsyncSession]
