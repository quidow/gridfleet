from __future__ import annotations

from typing import TYPE_CHECKING

from app.packs.services.ingest import (
    MAX_PACK_MANIFEST_BYTES,
    MAX_PACK_TARBALL_BYTES,
    MAX_PACK_TARBALL_MEMBERS,
    MAX_PACK_UNCOMPRESSED_BYTES,
    ingest_pack_tarball,
)
from app.packs.services.ingest import (
    PackIngestConflictError as PackUploadConflictError,
)
from app.packs.services.ingest import (
    PackIngestValidationError as PackUploadValidationError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.packs.models import DriverPack
    from app.packs.services.storage import PackStorageService

__all__ = [
    "MAX_PACK_MANIFEST_BYTES",
    "MAX_PACK_TARBALL_BYTES",
    "MAX_PACK_TARBALL_MEMBERS",
    "MAX_PACK_UNCOMPRESSED_BYTES",
    "PackUploadConflictError",
    "PackUploadValidationError",
    "upload_pack",
]


async def upload_pack(
    session: AsyncSession,
    *,
    storage: PackStorageService,
    username: str,
    origin_filename: str,
    data: bytes,
) -> DriverPack:
    return await ingest_pack_tarball(
        session,
        storage=storage,
        username=username,
        origin_filename=origin_filename,
        data=data,
    )
