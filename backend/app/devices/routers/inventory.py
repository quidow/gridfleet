from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.responses import StreamingResponse

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401
from app.devices.dependencies import DeviceServicesDep
from app.devices.routers.core import build_device_query_filters
from app.devices.schemas.filters import DeviceQueryFilters
from app.devices.schemas.inventory import (
    InventoryColumn,
    InventoryFormat,
    parse_columns_param,
)

router = APIRouter(
    prefix="/api/devices",
    tags=["devices"],
    responses={**RESPONSES_400, **RESPONSES_401},
)


def _parse_columns(columns: str | None = Query(default=None)) -> list[InventoryColumn]:
    try:
        return parse_columns_param(columns)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/inventory", summary="Read-only device inventory export (JSON or CSV)")
async def inventory(
    db: DbDep,
    device_services: DeviceServicesDep,
    fmt: InventoryFormat = Query(default=InventoryFormat.JSON, alias="format"),
    columns: list[InventoryColumn] = Depends(_parse_columns),
    filters: DeviceQueryFilters = Depends(build_device_query_filters),
) -> StreamingResponse:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    if fmt == InventoryFormat.CSV:
        media = "text/csv; charset=utf-8"
        filename = f"gridfleet-inventory-{stamp}.csv"
        iterator = device_services.inventory_export.iter_inventory_csv(db, columns=columns, filters=filters)
    else:
        media = "application/json"
        filename = f"gridfleet-inventory-{stamp}.json"
        iterator = device_services.inventory_export.iter_inventory_json(db, columns=columns, filters=filters)
    return StreamingResponse(
        iterator,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
