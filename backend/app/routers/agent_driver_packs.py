import uuid
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.pack_desired_state_service import compute_desired
from app.services.pack_status_service import apply_status

router = APIRouter(prefix="/agent/driver-packs", tags=["agent-driver-packs"])


@router.get("/desired")
async def desired(
    host_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    return await compute_desired(db, host_id)


@router.post("/status", status_code=204)
async def status(
    payload: dict[str, Any] = Body(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await apply_status(db, payload)
    await db.commit()
    return Response(status_code=204)
