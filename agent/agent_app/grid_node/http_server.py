from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

if TYPE_CHECKING:
    from starlette.requests import Request

    from agent_app.grid_node.node_state import NodeState


def build_app(*, state: NodeState, appium_upstream: str) -> Starlette:
    async def status(_request: Request) -> JSONResponse:
        snapshot = state.snapshot()
        return JSONResponse(
            {
                "value": {
                    "message": "GridFleet Python Grid Node",
                    "ready": True,
                    "slots": [
                        {
                            "id": slot.slot_id,
                            "state": slot.state,
                            "sessionId": slot.session_id,
                        }
                        for slot in snapshot.slots
                    ],
                }
            }
        )

    async def owner(request: Request) -> JSONResponse:
        session_id = request.path_params["session_id"]
        owned = any(slot.session_id == session_id for slot in state.snapshot().slots)
        return JSONResponse({"value": owned})

    return Starlette(
        routes=[
            Route("/status", status, methods=["GET"]),
            Route("/se/grid/node/owner/{session_id}", owner, methods=["POST"]),
        ]
    )
