from __future__ import annotations

import copy
import io
import tarfile
from typing import TYPE_CHECKING

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.driver_pack import DriverPack, DriverPackRelease
from app.schemas.driver_pack import PackOut
from app.services.auth_dependencies import require_admin
from app.services.pack_ingest_service import (
    PackIngestConflictError,
    PackIngestValidationError,
    ingest_pack_tarball,
)
from app.services.pack_release_ordering import selected_release
from app.services.pack_service import build_pack_out
from app.services.pack_storage_service import PackStorageService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])


class ForkPackBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_pack_id: str
    display_name: str | None = None


@router.post(
    "/{source_pack_id}/fork",
    response_model=PackOut,
    status_code=status.HTTP_201_CREATED,
)
async def fork(
    source_pack_id: str,
    body: ForkPackBody,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PackOut:
    existing = await session.get(DriverPack, body.new_pack_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"pack {body.new_pack_id!r} already exists")

    source = (
        await session.execute(
            select(DriverPack)
            .where(DriverPack.id == source_pack_id)
            .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail=f"source pack {source_pack_id!r} not found")

    src_release = selected_release(source.releases, source.current_release)
    if src_release is None:
        raise HTTPException(status_code=400, detail=f"source pack {source_pack_id!r} has no releases")

    forked_manifest = copy.deepcopy(src_release.manifest_json)
    forked_manifest["id"] = body.new_pack_id
    if body.display_name:
        forked_manifest["display_name"] = body.display_name
    forked_manifest["derived_from"] = {
        "pack_id": source.id,
        "release": src_release.release,
    }

    manifest_bytes = yaml.safe_dump(forked_manifest, sort_keys=False).encode("utf-8")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("manifest.yaml")
        info.size = len(manifest_bytes)
        info.mtime = 0
        info.uid = 0
        info.gid = 0
        info.uname = ""
        info.gname = ""
        tar.addfile(info, io.BytesIO(manifest_bytes))

    from app.config import settings

    storage = PackStorageService(settings.driver_pack_storage_dir)
    try:
        pack = await ingest_pack_tarball(
            session,
            storage=storage,
            username=_username,
            origin_filename=f"{body.new_pack_id}-fork.tar.gz",
            data=buf.getvalue(),
        )
    except PackIngestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackIngestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await session.commit()
    return build_pack_out(pack)
