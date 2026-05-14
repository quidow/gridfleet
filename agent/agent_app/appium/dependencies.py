"""FastAPI dependencies for ``/agent/appium/*`` routes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from fastapi import Depends

from agent_app.appium import appium_mgr as _module_appium_mgr

if TYPE_CHECKING:
    from agent_app.appium.process import AppiumProcessManager


def get_appium_mgr() -> AppiumProcessManager:
    """Return the module-level Appium process manager singleton.

    Tests override via ``app.dependency_overrides[get_appium_mgr] = lambda: fake``.
    """

    return _module_appium_mgr


AppiumMgrDep = Annotated["AppiumProcessManager", Depends(get_appium_mgr)]
