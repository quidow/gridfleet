"""Supported Python integration helpers for GridFleet."""

from importlib.metadata import PackageNotFoundError, version

from .appium import (
    build_appium_options,
    create_appium_driver,
    get_connection_target_from_driver,
    get_device_config_for_driver,
)
from .client import GRID_URL, GRIDFLEET_API_URL, GridFleetClient, HeartbeatThread, register_run_cleanup

try:
    __version__ = version("gridfleet-testkit")
except PackageNotFoundError:
    __version__ = "0.0.0"

__all__ = [
    "GRIDFLEET_API_URL",
    "GRID_URL",
    "GridFleetClient",
    "HeartbeatThread",
    "__version__",
    "build_appium_options",
    "create_appium_driver",
    "get_connection_target_from_driver",
    "get_device_config_for_driver",
    "register_run_cleanup",
]
