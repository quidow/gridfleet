"""FastAPI dependencies for ``/agent/tools/*``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from agent_app.tools.manager import get_tool_status


async def get_tool_status_dep() -> dict[str, Any]:
    return await get_tool_status()


ToolStatusDep = Annotated[dict[str, Any], Depends(get_tool_status_dep)]
