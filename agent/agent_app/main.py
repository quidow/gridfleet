"""GridFleet agent FastAPI app factory."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from agent_app.api_auth import BasicAuthMiddleware
from agent_app.appium.router import router as appium_router
from agent_app.config import agent_settings
from agent_app.error_codes import AgentErrorCode
from agent_app.grid_node.router import router as grid_node_router
from agent_app.host.router import router as host_router
from agent_app.lifespan import lifespan
from agent_app.observability import REQUEST_ID_HEADER, RequestContextMiddleware, configure_logging
from agent_app.pack.router import router as pack_router
from agent_app.plugins.router import router as plugins_router
from agent_app.terminal.router import router as terminal_router
from agent_app.tools.router import router as tools_router

configure_logging()

logger = logging.getLogger(__name__)

SHOW_DOCS_IN = {"local", "dev", "staging"}

app_kwargs: dict[str, Any] = {
    "title": "GridFleet Agent",
    "version": "0.1.0",
    "lifespan": lifespan,
}
if agent_settings.core.environment not in SHOW_DOCS_IN:
    app_kwargs["openapi_url"] = None

app = FastAPI(**app_kwargs)
app.add_middleware(BasicAuthMiddleware)  # inner: enforces Basic auth on /agent/*
app.add_middleware(RequestContextMiddleware)  # outer: binds request_id, runs first


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = request.headers.get(REQUEST_ID_HEADER, "")
    return JSONResponse(
        status_code=422,
        content={
            "detail": {
                "code": AgentErrorCode.INVALID_PAYLOAD.value,
                "message": "Request validation failed",
                "errors": exc.errors(),
            }
        },
        headers={REQUEST_ID_HEADER: request_id} if request_id else None,
    )


@app.exception_handler(Exception)
async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled exception in %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": {
                "code": AgentErrorCode.INTERNAL_ERROR.value,
                "message": "Internal server error",
            }
        },
    )


for _router in (
    host_router,
    appium_router,
    pack_router,
    grid_node_router,
    plugins_router,
    tools_router,
    terminal_router,
):
    app.include_router(_router)

__all__ = ["app"]
