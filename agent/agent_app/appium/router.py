"""HTTP routes for ``/agent/appium/*``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request, status

from agent_app.appium.dependencies import AppiumMgrDep
from agent_app.appium.schemas import (
    AppiumLogsResponse,
    AppiumStatusResponse,
    NodeRefreshResponse,
)
from agent_app.error_codes import ErrorEnvelope

router = APIRouter(prefix="/agent/appium", tags=["appium"])
node_state_router = APIRouter(prefix="/agent/appium-nodes", tags=["appium-nodes"])


@node_state_router.post(
    "/refresh",
    response_model=NodeRefreshResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Wake the desired Appium-node state poller",
    responses={status.HTTP_401_UNAUTHORIZED: {"model": ErrorEnvelope, "description": "UNAUTHORIZED"}},
)
async def refresh_node_state(request: Request) -> dict[str, bool]:
    loop = getattr(request.app.state, "node_state_loop", None)
    if loop is not None:
        loop.wake()
    return {"accepted": True}


@router.get(
    "/{port}/status",
    response_model=AppiumStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Process info for a managed Appium port",
)
async def appium_status(port: int, mgr: AppiumMgrDep) -> dict[str, Any]:
    return await mgr.status(port)


@router.get(
    "/{port}/logs",
    response_model=AppiumLogsResponse,
    status_code=status.HTTP_200_OK,
    summary="Recent stdout/stderr lines for a managed Appium",
)
async def appium_logs(port: int, mgr: AppiumMgrDep, lines: int = Query(100, ge=1, le=5000)) -> dict[str, Any]:
    log_lines = mgr.get_logs(port, lines=lines)
    return {"port": port, "lines": log_lines, "count": len(log_lines)}
