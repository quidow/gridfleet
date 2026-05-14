"""HTTP routes for ``/grid/node/*``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter

from agent_app.appium import appium_mgr
from agent_app.error_codes import AgentErrorCode, http_exc
from agent_app.grid_node.schemas import (
    GridNodeReregisterRequest,
    GridNodeReregisterResponse,
)

if TYPE_CHECKING:
    from agent_app.grid_node.supervisor import GridNodeServiceProtocol

router = APIRouter(prefix="/grid/node", tags=["grid"])


def _grid_node_service_for(node_id: str) -> GridNodeServiceProtocol:
    for supervisor in appium_mgr._grid_supervisors.values():
        service = supervisor.service
        if service is not None and service.node_id == node_id:
            return service
    raise http_exc(
        status_code=404,
        code=AgentErrorCode.DEVICE_NOT_FOUND,
        message=f"No running grid node is registered for node_id={node_id}",
    )


@router.post("/{node_id}/reregister", response_model=GridNodeReregisterResponse)
async def reregister_grid_node(node_id: str, payload: GridNodeReregisterRequest) -> GridNodeReregisterResponse:
    service = _grid_node_service_for(node_id)
    caps = service.slot_stereotype_caps()
    caps["gridfleet:run_id"] = str(payload.target_run_id) if payload.target_run_id is not None else "free"
    await service.reregister_with_stereotype(new_caps=caps)
    return GridNodeReregisterResponse(grid_run_id=payload.target_run_id)
