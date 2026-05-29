"""Pack domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.events.protocols import EventPublisher
    from app.packs.protocols import (
        FeatureProtocol,
        PackCatalogProtocol,
        PackLifecycleProtocol,
        PackReleaseProtocol,
        PackStatusProtocol,
    )
    from app.packs.services.storage import PackStorageService


@dataclass(frozen=True, slots=True)
class PackServices:
    catalog: PackCatalogProtocol
    release: PackReleaseProtocol
    status: PackStatusProtocol
    lifecycle: PackLifecycleProtocol
    feature: FeatureProtocol
    storage: PackStorageService
    publisher: EventPublisher
    circuit_breaker: CircuitBreakerProtocol
    session_factory: async_sessionmaker[AsyncSession]
