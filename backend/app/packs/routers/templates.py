from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.auth.dependencies import AdminDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.core.dependencies import DbDep  # noqa: TC001 - FastAPI inspects dependency aliases at runtime.
from app.packs import packs_settings
from app.packs.models import DriverPack
from app.packs.schemas import PackOut
from app.packs.services.ingest import (
    PackIngestConflictError,
    PackIngestValidationError,
    ingest_pack_tarball,
)
from app.packs.services.service import build_pack_out
from app.packs.services.storage import PackStorageService
from app.packs.services.template import (
    build_tarball_from_template,
    list_templates,
    load_template,
)

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
    _username: AdminDep,
) -> dict[str, Any]:
    descriptors = list_templates()
    return {
        "templates": [
            {
                "template_id": descriptor.id,
                "display_name": descriptor.display_name,
                "target_driver_summary": descriptor.target_driver_summary,
                "source_pack_id": descriptor.source_pack_id,
                "prerequisite_host_tools": list(descriptor.prerequisite_host_tools),
            }
            for descriptor in descriptors
        ]
    }


@router.post(
    "/from-template/{template_id}",
    response_model=PackOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_from_template(
    template_id: str,
    body: FromTemplateBody,
    _username: AdminDep,
    session: DbDep,
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

    storage = PackStorageService(packs_settings.driver_pack_storage_dir)
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
