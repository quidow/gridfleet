from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.timeutil import now_utc
from app.portability.dependencies import PortabilityServicesDep
from app.portability.schemas import (
    ExportBundle,
    ImportCommitRequest,
    ImportCommitResult,
    ImportPreview,
)
from app.portability.services.import_bundle import (
    BundleHashMismatchError,
    GroupKeyCollisionError,
    UnknownGroupReferenceError,
)

router = APIRouter(
    prefix="/api/portability",
    tags=["portability"],
    responses=STANDARD_ERROR_RESPONSES,
)


@router.get("/export", response_model=ExportBundle, summary="Export all registered devices as a portable JSON bundle")
async def export_devices(db: DbDep, portability_services: PortabilityServicesDep, response: Response) -> ExportBundle:
    bundle = await portability_services.export.build_export_bundle(db)
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    response.headers["Content-Disposition"] = f'attachment; filename="gridfleet-devices-{stamp}.json"'
    return bundle


@router.post(
    "/import/validate",
    response_model=ImportPreview,
    summary="Validate a device import bundle and return a per-row preview",
)
async def import_validate(
    bundle: ExportBundle, db: DbDep, portability_services: PortabilityServicesDep
) -> ImportPreview:
    try:
        return await portability_services.import_.validate_bundle(db, bundle)
    except GroupKeyCollisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownGroupReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/import", response_model=ImportCommitResult, summary="Commit a previously-validated device import bundle")
async def import_commit(
    request: ImportCommitRequest, db: DbDep, portability_services: PortabilityServicesDep
) -> ImportCommitResult:
    try:
        return await portability_services.import_.commit_import(db, request)
    except BundleHashMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except GroupKeyCollisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnknownGroupReferenceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
