"""HTTP routes for ``/grid/node/*``."""

from fastapi import APIRouter

from agent_app.grid_node.dependencies import GridNodeServiceDep
from agent_app.grid_node.schemas import (
    GridNodeReregisterRequest,
    GridNodeReregisterResponse,
)

router = APIRouter(prefix="/grid/node", tags=["grid"])


@router.post("/{node_id}/reregister", response_model=GridNodeReregisterResponse)
async def reregister_grid_node(
    service: GridNodeServiceDep,
    payload: GridNodeReregisterRequest,
) -> GridNodeReregisterResponse:
    caps = service.slot_stereotype_caps()
    caps["gridfleet:run_id"] = str(payload.target_run_id) if payload.target_run_id is not None else "free"
    await service.reregister_with_stereotype(new_caps=caps)
    return GridNodeReregisterResponse(grid_run_id=payload.target_run_id)
