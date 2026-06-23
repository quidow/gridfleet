import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_401, RESPONSES_404
from app.devices.schemas.lifecycle import LifecycleIncidentListRead
from app.lifecycle.dependencies import LifecycleServicesDep

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"], responses={**RESPONSES_401, **RESPONSES_404})


@router.get("/incidents", response_model=LifecycleIncidentListRead)
async def get_lifecycle_incidents(
    db: DbDep,
    lifecycle_services: LifecycleServicesDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    device_id: Annotated[uuid.UUID | None, Query()] = None,
    cursor: Annotated[str | None, Query()] = None,
    direction: Annotated[str, Query()] = "older",
) -> dict[str, Any]:
    items, next_cursor, prev_cursor = await lifecycle_services.incidents.list_lifecycle_incidents_paginated(
        db, limit=limit, device_id=device_id, cursor=cursor, direction=direction
    )
    return {"items": items, "limit": limit, "next_cursor": next_cursor, "prev_cursor": prev_cursor}
