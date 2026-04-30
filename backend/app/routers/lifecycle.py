import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.lifecycle import LifecycleIncidentListRead
from app.services import lifecycle_incident_service

router = APIRouter(prefix="/api/lifecycle", tags=["lifecycle"])


@router.get("/incidents", response_model=LifecycleIncidentListRead)
async def get_lifecycle_incidents(
    limit: int = Query(50, ge=1, le=200),
    device_id: uuid.UUID | None = Query(None),
    cursor: str | None = Query(None),
    direction: str = Query("older"),
    db: AsyncSession = Depends(get_db),
) -> LifecycleIncidentListRead:
    items, next_cursor, prev_cursor = await lifecycle_incident_service.list_lifecycle_incidents_paginated(
        db, limit=limit, device_id=device_id, cursor=cursor, direction=direction
    )
    return LifecycleIncidentListRead(
        items=items,
        limit=limit,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
    )
