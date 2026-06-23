from __future__ import annotations

from typing import TYPE_CHECKING

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import envelope_response

if TYPE_CHECKING:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse


def register(app: FastAPI) -> None:
    @app.exception_handler(NodeManagerError)
    async def handle_node_manager_error(request: Request, exc: NodeManagerError) -> JSONResponse:
        return envelope_response(
            status_code=400,
            code="VALIDATION_ERROR",
            message=str(exc),
            request_id=getattr(request.state, "request_id", None),
        )
