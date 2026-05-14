"""HTTP routes for ``/agent/plugins``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status

from agent_app.plugins.dependencies import (  # noqa: TC001 - FastAPI resolves at runtime
    InstalledPluginsDep,
    SyncPluginsResultDep,
)
from agent_app.plugins.schemas import PluginListItem, PluginSyncResponse

router = APIRouter(prefix="/agent/plugins", tags=["plugins"])


@router.get(
    "",
    response_model=list[PluginListItem],
    status_code=status.HTTP_200_OK,
    summary="List installed Appium plugins",
)
async def list_plugins(installed: InstalledPluginsDep) -> list[dict[str, str]]:
    return installed


@router.post(
    "/sync",
    response_model=PluginSyncResponse,
    status_code=status.HTTP_200_OK,
    summary="Sync the installed plugin set",
)
async def sync_agent_plugins(result: SyncPluginsResultDep) -> dict[str, Any]:
    return result
