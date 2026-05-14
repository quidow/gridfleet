"""FastAPI dependencies for ``/grid/node/*`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from agent_app.appium import appium_mgr
from agent_app.error_codes import AgentErrorCode, http_exc

if TYPE_CHECKING:
    from agent_app.grid_node.supervisor import GridNodeServiceProtocol


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


GridNodeServiceDep = Annotated["GridNodeServiceProtocol", Depends(_grid_node_service_for)]
