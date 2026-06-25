"""Run domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.runs.protocols import RunReleaseProtocol, RunReservationProtocol
    from app.runs.service_allocator import RunAllocatorService
    from app.runs.service_lifecycle import RunLifecycleService
    from app.runs.service_lifecycle_failures import RunFailureService
    from app.runs.service_query import RunQueryService


@dataclass(frozen=True, slots=True)
class RunServices:
    allocator: RunAllocatorService
    lifecycle: RunLifecycleService
    release: RunReleaseProtocol
    failure: RunFailureService
    reservation: RunReservationProtocol
    query: RunQueryService
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
