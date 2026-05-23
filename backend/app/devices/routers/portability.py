from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404, RESPONSES_409
from app.devices.schemas.portability import ExportBundle, ImportCommitRequest, ImportCommitResult, ImportPreview
from app.devices.services.portability_export import build_export_bundle
from app.devices.services.portability_import import BundleHashMismatchError, commit_import, validate_bundle

router = APIRouter(
    prefix="/api/devices",
    tags=["devices"],
    responses={**RESPONSES_400, **RESPONSES_401, **RESPONSES_404, **RESPONSES_409},
)


@router.get(
    "/export",
    response_model=ExportBundle,
    summary="Export all registered devices as a portable JSON bundle",
)
async def export_devices(db: DbDep, response: Response) -> ExportBundle:
    bundle = await build_export_bundle(db)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    response.headers["Content-Disposition"] = f'attachment; filename="gridfleet-devices-{stamp}.json"'
    return bundle


@router.post(
    "/import/validate",
    response_model=ImportPreview,
    summary="Validate a device import bundle and return a per-row preview",
)
async def import_validate(bundle: ExportBundle, db: DbDep) -> ImportPreview:
    try:
        return await validate_bundle(db, bundle)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/import",
    response_model=ImportCommitResult,
    summary="Commit a previously-validated device import bundle",
)
async def import_commit(request: ImportCommitRequest, db: DbDep) -> ImportCommitResult:
    try:
        return await commit_import(db, request)
    except BundleHashMismatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
