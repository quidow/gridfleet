"""Run domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.runs.service_allocator import RunAllocatorService
    from app.runs.service_lifecycle import RunLifecycleService
    from app.runs.service_lifecycle_failures import RunFailureService
    from app.runs.service_lifecycle_release import RunReleaseService
    from app.runs.service_query import RunQueryService
    from app.runs.service_reservation import RunReservationService


@dataclass(frozen=True, slots=True)
class RunServices:
    allocator: RunAllocatorService
    lifecycle: RunLifecycleService
    release: RunReleaseService
    failure: RunFailureService
    reservation: RunReservationService
    query: RunQueryService
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
