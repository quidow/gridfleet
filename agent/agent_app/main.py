"""GridFleet agent FastAPI app factory."""

from fastapi import FastAPI

from agent_app.api_auth import BasicAuthMiddleware
from agent_app.appium.router import router as appium_router
from agent_app.grid_node.router import router as grid_node_router
from agent_app.host.router import router as host_router
from agent_app.lifespan import lifespan
from agent_app.observability import RequestContextMiddleware, configure_logging
from agent_app.pack.router import router as pack_router
from agent_app.plugins.router import router as plugins_router
from agent_app.terminal.router import router as terminal_router
from agent_app.tools.router import router as tools_router

configure_logging()

app = FastAPI(title="GridFleet Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(BasicAuthMiddleware)  # inner: enforces Basic auth on /agent/*
app.add_middleware(RequestContextMiddleware)  # outer: binds request_id, runs first

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
