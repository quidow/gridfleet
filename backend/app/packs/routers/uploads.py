from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.auth.dependencies import AdminDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.core.dependencies import DbDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.packs import packs_settings
from app.packs.models import DriverPackRelease
from app.packs.schemas import CurrentReleasePatch, PackOut, PackReleasesOut
from app.packs.services import release as pack_release_service
from app.packs.services.service import build_pack_out
from app.packs.services.storage import PackStorageService
from app.packs.services.upload import (
    MAX_PACK_TARBALL_BYTES,
    PackUploadConflictError,
    PackUploadValidationError,
    upload_pack,
)

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


def get_pack_storage() -> PackStorageService:
    """FastAPI dependency that returns a PackStorageService rooted at the configured dir.

    Override ``app.dependency_overrides[get_pack_storage]`` in tests to point at
    a writable ``tmp_path``-rooted instance instead of the production storage dir.
    """
    return PackStorageService(root=packs_settings.driver_pack_storage_dir)


PackStorageDep = Annotated[PackStorageService, Depends(get_pack_storage)]


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
    username: AdminDep,
    session: DbDep,
    storage: PackStorageDep,
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
    session: DbDep,
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
async def list_releases(pack_id: str, session: DbDep) -> PackReleasesOut:
    result = await pack_release_service.list_releases(session, pack_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Pack {pack_id!r} not found")
    return result


@router.patch("/{pack_id}/releases/current", response_model=PackOut)
async def update_current_release(
    pack_id: str,
    body: CurrentReleasePatch,
    _username: AdminDep,
    session: DbDep,
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
    _username: AdminDep,
    session: DbDep,
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
