"""HTTP routes for ``/agent/plugins``."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from agent_app.plugins.manager import get_installed_plugins, sync_plugins
from agent_app.plugins.schemas import PluginSyncRequest  # noqa: TC001 - FastAPI resolves at runtime

router = APIRouter(prefix="/agent/plugins", tags=["plugins"])


@router.get("", summary="List installed Appium plugins")
async def list_plugins() -> list[dict[str, str]]:
    return await get_installed_plugins()


@router.post("/sync", summary="Sync the installed plugin set")
async def sync_agent_plugins(req: PluginSyncRequest) -> dict[str, Any]:
    configs = [plugin.model_dump() for plugin in req.plugins]
    return await sync_plugins(configs)
