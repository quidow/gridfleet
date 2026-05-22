from datetime import UTC, datetime

from fastapi import APIRouter, Response

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404, RESPONSES_409
from app.devices.schemas.portability import ExportBundle
from app.devices.services.portability_export import build_export_bundle

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
