"""Appium subsystem.

Owns the long-lived ``AppiumProcessManager`` singleton (``appium_mgr``)
plus the FastAPI router for ``/agent/appium/*`` endpoints.
"""

from agent_app.appium.process import AppiumProcessManager

appium_mgr = AppiumProcessManager()

__all__ = ["appium_mgr"]
