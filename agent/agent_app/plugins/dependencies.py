"""FastAPI dependencies for ``/agent/plugins/*``."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import Depends

from agent_app.plugins.manager import get_installed_plugins, sync_plugins


async def get_installed_plugins_dep() -> list[dict[str, str]]:
    return await get_installed_plugins()


def sync_plugins_dep() -> Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]]:
    return sync_plugins


InstalledPluginsDep = Annotated[list[dict[str, str]], Depends(get_installed_plugins_dep)]
SyncPluginsDep = Annotated[
    Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any]]],
    Depends(sync_plugins_dep),
]
