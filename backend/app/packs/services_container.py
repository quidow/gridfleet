"""Pack domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.packs.protocols import PackDiscoveryProtocol
    from app.packs.services.lifecycle import PackLifecycleService
    from app.packs.services.release import PackReleaseService
    from app.packs.services.service import PackCatalogService
    from app.packs.services.status import PackStatusService
    from app.packs.services.storage import PackStorageService


@dataclass(frozen=True, slots=True)
class PackServices:
    catalog: PackCatalogService
    release: PackReleaseService
    status: PackStatusService
    lifecycle: PackLifecycleService
    discovery: PackDiscoveryProtocol
    storage: PackStorageService
    session_factory: async_sessionmaker[AsyncSession]
