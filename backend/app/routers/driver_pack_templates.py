from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.database import get_db
from app.models.driver_pack import DriverPack
from app.schemas.driver_pack import PackOut
from app.services.auth_dependencies import require_admin
from app.services.pack_ingest_service import (
    PackIngestConflictError,
    PackIngestValidationError,
    ingest_pack_tarball,
)
from app.services.pack_service import build_pack_out
from app.services.pack_storage_service import PackStorageService
from app.services.pack_template_service import (
    build_tarball_from_template,
    list_templates,
    load_template,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/driver-packs", tags=["driver-packs"])


class TemplateDescriptorOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str
    display_name: str
    target_driver_summary: str
    source_pack_id: str
    prerequisite_host_tools: list[str]


class TemplatesList(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates: list[TemplateDescriptorOut]


class FromTemplateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pack_id: str
    release: str
    display_name: str | None = None


@router.get("/templates", response_model=TemplatesList)
async def get_templates(
    _username: str = Depends(require_admin),
) -> TemplatesList:
    descriptors = list_templates()
    return TemplatesList(
        templates=[
            TemplateDescriptorOut(
                template_id=descriptor.id,
                display_name=descriptor.display_name,
                target_driver_summary=descriptor.target_driver_summary,
                source_pack_id=descriptor.source_pack_id,
                prerequisite_host_tools=list(descriptor.prerequisite_host_tools),
            )
            for descriptor in descriptors
        ]
    )


@router.post(
    "/from-template/{template_id}",
    response_model=PackOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_from_template(
    template_id: str,
    body: FromTemplateBody,
    _username: str = Depends(require_admin),
    session: AsyncSession = Depends(get_db),
) -> PackOut:
    existing = await session.get(DriverPack, body.pack_id)
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"pack {body.pack_id!r} already exists")

    try:
        template = load_template(template_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    tarball_data = build_tarball_from_template(
        template,
        pack_id=body.pack_id,
        release=body.release,
        display_name=body.display_name,
    )

    from app.config import settings

    storage = PackStorageService(settings.driver_pack_storage_dir)
    try:
        pack = await ingest_pack_tarball(
            session,
            storage=storage,
            username=_username,
            origin_filename=f"{body.pack_id}-from-template.tar.gz",
            data=tarball_data,
        )
    except PackIngestConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PackIngestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await session.commit()
    return build_pack_out(pack)
