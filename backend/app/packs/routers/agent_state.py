import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

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
