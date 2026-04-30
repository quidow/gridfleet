from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.config import settings
from app.database import get_db
from app.models.driver_pack import DriverPackRelease
from app.schemas.driver_pack import CurrentReleasePatch, PackOut, PackReleasesOut
from app.services import pack_release_service
from app.services.auth_dependencies import require_admin
from app.services.pack_service import build_pack_out
from app.services.pack_storage_service import PackStorageService
from app.services.pack_upload_service import (
    MAX_PACK_TARBALL_BYTES,
    PackUploadConflictError,
    PackUploadValidationError,
    upload_pack,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def get_pack_storage() -> PackStorageService:
    """FastAPI dependency that returns a PackStorageService rooted at the configured dir.

    Override ``app.dependency_overrides[get_pack_storage]`` in tests to point at
    a writable ``tmp_path``-rooted instance instead of the production storage dir.
    """
    return PackStorageService(root=settings.driver_pack_storage_dir)


async def _read_limited_upload(tarball: UploadFile) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await tarball.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_PACK_TARBALL_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"tarball exceeds maximum size of {MAX_PACK_TARBALL_BYTES} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/uploads", response_model=PackOut, status_code=status.HTTP_201_CREATED)
async def upload(
    tarball: UploadFile,
    username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
    storage: PackStorageService = Depends(get_pack_storage),
) -> PackOut:
    data = await _read_limited_upload(tarball)
    if not data:
        raise HTTPException(status_code=400, detail="empty tarball")
    try:
        pack = await upload_pack(
            session,
            storage=storage,
            username=username,
            origin_filename=tarball.filename or "unknown.tar.gz",
            data=data,
        )
    except PackUploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PackUploadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return build_pack_out(pack)


@router.get("/{pack_id}/releases/{release}/tarball")
async def fetch_tarball(
    pack_id: str,
    release: str,
    session: AsyncSession = Depends(get_db),
) -> FileResponse:
    record = (
        await session.execute(
            select(DriverPackRelease).where(
                DriverPackRelease.pack_id == pack_id,
                DriverPackRelease.release == release,
            )
        )
    ).scalar_one_or_none()
    if record is None or record.artifact_path is None:
        raise HTTPException(status_code=404, detail="release artifact not found")
    if not Path(record.artifact_path).is_file():
        raise HTTPException(status_code=404, detail="release artifact not found")
    return FileResponse(record.artifact_path, media_type="application/gzip")


@router.get("/{pack_id}/releases", response_model=PackReleasesOut)
async def list_releases(pack_id: str, session: AsyncSession = Depends(get_db)) -> PackReleasesOut:
    result = await pack_release_service.list_releases(session, pack_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found")
    return result


@router.patch("/{pack_id}/releases/current", response_model=PackOut)
async def update_current_release(
    pack_id: str,
    body: CurrentReleasePatch,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PackOut:
    try:
        pack = await pack_release_service.set_current_release(session, pack_id, body.release)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await session.commit()
    return build_pack_out(pack)


@router.delete("/{pack_id}/releases/{release}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_release(
    pack_id: str,
    release: str,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> Response:
    try:
        await pack_release_service.delete_release(session, pack_id, release)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
