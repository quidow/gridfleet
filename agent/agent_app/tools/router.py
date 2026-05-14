"""HTTP routes for ``/agent/tools``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status

from agent_app.tools.manager import get_tool_status
from agent_app.tools.schemas import ToolsStatusResponse

router = APIRouter(prefix="/agent/tools", tags=["tools"])


@router.get(
    "/status",
    response_model=ToolsStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Detected versions of supporting CLI tools",
)
async def agent_tools_status() -> dict[str, Any]:
    return await get_tool_status()
