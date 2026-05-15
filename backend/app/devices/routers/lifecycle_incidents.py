import uuid
from typing import Any

from fastapi import APIRouter, Query

from app.core.dependencies import DbDep
from app.devices.schemas.lifecycle import LifecycleIncidentListRead
from app.devices.services import lifecycle_incidents as lifecycle_incident_service

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])


@router.get("/incidents", response_model=LifecycleIncidentListRead)
async def get_lifecycle_incidents(
    db: DbDep,
    limit: int = Query(50, ge=1, le=200),
    device_id: uuid.UUID | None = Query(None),
    cursor: str | None = Query(None),
    direction: str = Query("older"),
) -> dict[str, Any]:
    items, next_cursor, prev_cursor = await lifecycle_incident_service.list_lifecycle_incidents_paginated(
        db, limit=limit, device_id=device_id, cursor=cursor, direction=direction
    )
    return {"items": items, "limit": limit, "next_cursor": next_cursor, "prev_cursor": prev_cursor}
