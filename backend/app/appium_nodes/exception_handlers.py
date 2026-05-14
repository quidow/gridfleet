from __future__ import annotations

from fastapi import FastAPI, Request  # noqa: TC002 - FastAPI handler registration inspects annotations.
from fastapi.responses import JSONResponse  # noqa: TC002 - FastAPI handler registration inspects annotations.

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import error_response, request_id_from_request


def register(app: FastAPI) -> None:
    @app.exception_handler(NodeManagerError)
    async def handle_node_manager_error(request: Request, exc: NodeManagerError) -> JSONResponse:
        return error_response(
            status_code=400,
            code="VALIDATION_ERROR",
            message=str(exc),
            request_id=request_id_from_request(request),
        )
