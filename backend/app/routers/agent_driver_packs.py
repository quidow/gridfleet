import uuid
from typing import Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import Response

from app.dependencies import DbDep
from app.services.pack_desired_state_service import compute_desired
from app.services.pack_status_service import apply_status

router = APIRouter(prefix="/agent/driver-packs", tags=["agent-driver-packs"])


@router.get("/desired")
async def desired(
    db: DbDep,
    host_id: uuid.UUID = Query(...),
) -> dict[str, Any]:
    return await compute_desired(db, host_id)


@router.post("/status", status_code=204)
async def status(
    db: DbDep,
    payload: dict[str, Any] = Body(...),
) -> Response:
    await apply_status(db, payload)
    await db.commit()
    return Response(status_code=204)
