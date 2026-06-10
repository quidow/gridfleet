from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.auth.dependencies import AdminDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.core.dependencies import DbDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.core.http_errors import convert_not_found, found_or_404
from app.packs.dependencies import PackServicesDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.packs.models import DriverPackRelease
from app.packs.schemas import CurrentReleasePatch, PackOut, PackReleasesOut
from app.packs.services.ingest import (
    MAX_PACK_TARBALL_BYTES,
)
from app.packs.services.ingest import (
    PackIngestConflictError as PackUploadConflictError,
)
from app.packs.services.ingest import (
    PackIngestValidationError as PackUploadValidationError,
)
from app.packs.services.service import build_pack_out

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])
UPLOAD_READ_CHUNK_BYTES = 1024 * 1024


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
    packs: PackServicesDep,
) -> PackOut:
    data = await _read_limited_upload(tarball)
    if not data:
        raise HTTPException(status_code=400, detail="empty tarball")
    try:
        pack = await packs.release.upload(
            session,
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
async def list_releases(pack_id: str, session: DbDep, packs: PackServicesDep) -> PackReleasesOut:
    return found_or_404(await packs.release.list_releases(session, pack_id), f"Pack {pack_id!r} not found")


@router.patch("/{pack_id}/releases/current", response_model=PackOut)
async def update_current_release(
    pack_id: str,
    body: CurrentReleasePatch,
    _username: AdminDep,
    session: DbDep,
    packs: PackServicesDep,
) -> PackOut:
    with convert_not_found():
        pack = await packs.release.set_current_release(session, pack_id, body.release)
    await session.commit()
    return build_pack_out(pack)


@router.delete("/{pack_id}/releases/{release}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_release(
    pack_id: str,
    release: str,
    _username: AdminDep,
    session: DbDep,
    packs: PackServicesDep,
) -> Response:
    try:
        await packs.release.delete_release(session, pack_id, release)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
