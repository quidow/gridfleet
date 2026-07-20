from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from starlette.responses import StreamingResponse

from app.core.dependencies import DbDep
from app.core.error_responses import STANDARD_ERROR_RESPONSES
from app.core.timeutil import now_utc
from app.devices.routers.core import build_device_query_filters
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.services.service import UnknownGroupKeysError
from app.portability.dependencies import PortabilityServicesDep
from app.portability.schemas import (
    ExportBundle,
    ImportCommitRequest,
    ImportCommitResult,
    ImportPreview,
    InventoryColumn,
    InventoryFormat,
    parse_columns_param,
)
from app.portability.services.import_bundle import (
    BundleHashMismatchError,
    GroupKeyCollisionError,
    UnknownGroupReferenceError,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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


def _parse_columns(columns: str | None = Query(default=None)) -> list[InventoryColumn]:
    try:
        return parse_columns_param(columns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/inventory", summary="Read-only device inventory export (JSON or CSV)")
async def inventory(
    db: DbDep,
    portability_services: PortabilityServicesDep,
    columns: Annotated[list[InventoryColumn], Depends(_parse_columns)],
    filters: Annotated[DeviceQueryFilters, Depends(build_device_query_filters)],
    fmt: Annotated[InventoryFormat, Query(alias="format")] = InventoryFormat.JSON,
) -> StreamingResponse:
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    if fmt == InventoryFormat.CSV:
        media = "text/csv; charset=utf-8"
        filename = f"gridfleet-inventory-{stamp}.csv"
        iterator = portability_services.inventory.iter_inventory_csv(db, columns=columns, filters=filters)
    else:
        media = "application/json"
        filename = f"gridfleet-inventory-{stamp}.json"
        iterator = portability_services.inventory.iter_inventory_json(db, columns=columns, filters=filters)
    # Pull the first chunk eagerly so an unknown group key surfaces as 422 before
    # the response starts streaming. ``anext(..., None)`` rather than ``__anext__``:
    # a generator that returns without yielding is an empty body, not a 500.
    try:
        first_chunk = await anext(iterator, None)
    except UnknownGroupKeysError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    async def chained() -> AsyncIterator[str]:
        if first_chunk is None:
            return
        yield first_chunk
        async for chunk in iterator:
            yield chunk

    return StreamingResponse(
        chained(),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
