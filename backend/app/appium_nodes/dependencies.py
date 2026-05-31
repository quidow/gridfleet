"""Appium-node-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request

if TYPE_CHECKING:
    from app.appium_nodes.services_container import AppiumNodeServices


def get_appium_node_services(request: Request) -> AppiumNodeServices:
    return request.app.state.services.appium_nodes  # type: ignore[no-any-return]


AppiumNodeServicesDep = Annotated["AppiumNodeServices", Depends(get_appium_node_services)]
