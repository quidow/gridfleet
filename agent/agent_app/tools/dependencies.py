"""FastAPI dependencies for ``/agent/tools/*``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request

from agent_app.tools.manager import get_tool_status


async def get_tool_status_dep(request: Request) -> dict[str, Any]:
    registry = getattr(request.app.state, "adapter_registry", None)
    return await get_tool_status(adapter_registry=registry)


ToolStatusDep = Annotated[dict[str, Any], Depends(get_tool_status_dep)]
