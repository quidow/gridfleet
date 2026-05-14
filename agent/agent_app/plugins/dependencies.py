"""FastAPI dependencies for ``/agent/plugins/*``."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends

from agent_app.plugins.manager import get_installed_plugins, sync_plugins
from agent_app.plugins.schemas import PluginSyncRequest  # noqa: TC001 - FastAPI resolves at runtime


async def get_installed_plugins_dep() -> list[dict[str, str]]:
    return await get_installed_plugins()


async def sync_plugins_dep(req: PluginSyncRequest) -> dict[str, Any]:
    configs = [plugin.model_dump() for plugin in req.plugins]
    return await sync_plugins(configs)


InstalledPluginsDep = Annotated[list[dict[str, str]], Depends(get_installed_plugins_dep)]
SyncPluginsResultDep = Annotated[dict[str, Any], Depends(sync_plugins_dep)]
