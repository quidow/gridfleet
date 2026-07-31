import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query

from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_401, RESPONSES_404
from app.core.pagination import CursorPaginationError
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
    direction: Annotated[Literal["older", "newer"], Query()] = "older",
    scope: Annotated[Literal["all", "policy"], Query()] = "all",
) -> dict[str, Any]:
    try:
        page = await lifecycle_services.incidents.list_lifecycle_incidents_paginated(
            db, limit=limit, device_id=device_id, cursor=cursor, direction=direction, scope=scope
        )
    except CursorPaginationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "items": page.items,
        "limit": page.limit,
        "next_cursor": page.next_cursor,
        "prev_cursor": page.prev_cursor,
    }
