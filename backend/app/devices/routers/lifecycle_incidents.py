import uuid
from typing import Any

from fastapi import APIRouter, Query

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_401, RESPONSES_404
from app.devices.dependencies import DeviceServicesDep
from app.devices.schemas.lifecycle import LifecycleIncidentListRead

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"], responses={**RESPONSES_401, **RESPONSES_404})


@router.get("/incidents", response_model=LifecycleIncidentListRead)
async def get_lifecycle_incidents(
    db: DbDep,
    device_services: DeviceServicesDep,
    limit: int = Query(50, ge=1, le=200),
    device_id: uuid.UUID | None = Query(None),
    cursor: str | None = Query(None),
    direction: str = Query("older"),
) -> dict[str, Any]:
    items, next_cursor, prev_cursor = await device_services.lifecycle_incidents.list_lifecycle_incidents_paginated(
        db, limit=limit, device_id=device_id, cursor=cursor, direction=direction
    )
    return {"items": items, "limit": limit, "next_cursor": next_cursor, "prev_cursor": prev_cursor}
