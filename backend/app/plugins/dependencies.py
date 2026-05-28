"""Plugins-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.plugins.services_container import PluginServices


def get_plugin_services(request: Request) -> PluginServices:
    return request.app.state.services.plugins  # type: ignore[no-any-return]


PluginServicesDep = Annotated["PluginServices", Depends(get_plugin_services)]
