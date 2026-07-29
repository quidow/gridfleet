from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.auth.dependencies import AdminDep
from app.core.dependencies import DbDep
from app.core.http_errors import found_or_404
from app.packs.dependencies import PackServicesDep
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
from app.packs.services.service import PackNotFound, build_pack_out, purge_pack_artifacts

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
    packs: PackServicesDep,
) -> PackOut:
    # Read and size-cap the body before any boundary opens: no transaction may
    # span the upload stream.
    data = await _read_limited_upload(tarball)
    if not data:
        raise HTTPException(status_code=400, detail="empty tarball")
    try:
        return await packs.release.upload(
            packs.session_factory,
            username=username,
            origin_filename=tarball.filename or "unknown.tar.gz",
            data=data,
        )
    except PackUploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PackUploadConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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
    packs: PackServicesDep,
) -> PackOut:
    # PackNotFound by name, not LookupError: build_pack_out runs inside the
    # transaction and indexes persisted manifest/platform data, so a KeyError
    # from a malformed row must stay a 500 instead of becoming a 404.
    try:
        async with packs.session_factory.begin() as db:
            pack = await packs.release.set_current_release(db, pack_id, body.release)
            return build_pack_out(pack)
    except PackNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/{pack_id}/releases/{release}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_release(
    pack_id: str,
    release: str,
    _username: AdminDep,
    packs: PackServicesDep,
) -> Response:
    try:
        async with packs.session_factory.begin() as db:
            artifact_path = await packs.release.delete_release(db, pack_id, release)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    # Post-commit, for the same reason as the pack delete: no transaction and no
    # pack row lock may span filesystem deletion, and nothing after the commit
    # may fail the response. On success the ledger row goes with the file; on
    # failure it stays ``orphaned`` and the janitor's reaper comes back for it.
    if artifact_path:
        await purge_pack_artifacts(packs.session_factory, [artifact_path])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
