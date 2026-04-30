from __future__ import annotations

from typing import TYPE_CHECKING

from app.services.pack_ingest_service import (
    MAX_PACK_MANIFEST_BYTES,
    MAX_PACK_TARBALL_BYTES,
    MAX_PACK_TARBALL_MEMBERS,
    MAX_PACK_UNCOMPRESSED_BYTES,
    ingest_pack_tarball,
)
from app.services.pack_ingest_service import (
    PackIngestConflictError as PackUploadConflictError,
)
from app.services.pack_ingest_service import (
    PackIngestValidationError as PackUploadValidationError,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.driver_pack import DriverPack
    from app.services.pack_storage_service import PackStorageService

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
