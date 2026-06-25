"""Pack domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.events.protocols import EventPublisher
    from app.packs.protocols import PackDiscoveryProtocol, PackLifecycleProtocol
    from app.packs.services.feature_dispatch import FeatureService
    from app.packs.services.release import PackReleaseService
    from app.packs.services.service import PackCatalogService
    from app.packs.services.status import PackStatusService
    from app.packs.services.storage import PackStorageService


@dataclass(frozen=True, slots=True)
class PackServices:
    catalog: PackCatalogService
    release: PackReleaseService
    status: PackStatusService
    lifecycle: PackLifecycleProtocol
    feature: FeatureService
    discovery: PackDiscoveryProtocol
    storage: PackStorageService
    publisher: EventPublisher
    circuit_breaker: CircuitBreakerProtocol
    session_factory: async_sessionmaker[AsyncSession]
