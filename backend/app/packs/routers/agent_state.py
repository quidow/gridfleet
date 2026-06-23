import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query
from fastapi.responses import Response

from app.core.dependencies import DbDep
from app.packs.dependencies import PackServicesDep

router = APIRouter(prefix="/agent/driver-packs", tags=["agent-driver-packs"])


@router.get("/desired")
async def desired(
    db: DbDep,
    packs: PackServicesDep,
    host_id: Annotated[uuid.UUID, Query()],
) -> dict[str, Any]:
    return await packs.status.compute_desired(db, host_id)


@router.post("/status", status_code=204)
async def status(
    db: DbDep,
    packs: PackServicesDep,
    payload: Annotated[dict[str, Any], Body()],
) -> Response:
    await packs.status.apply_status(db, payload)
    await db.commit()
    return Response(status_code=204)
