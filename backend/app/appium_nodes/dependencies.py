"""Appium-node-domain FastAPI dependencies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from app.core.service_deps import make_services_getter

if TYPE_CHECKING:
    from app.appium_nodes.services_container import AppiumNodeServices

get_appium_node_services = make_services_getter("appium_nodes")
AppiumNodeServicesDep = Annotated["AppiumNodeServices", Depends(get_appium_node_services)]
