"""Supported Python integration helpers for GridFleet."""

from importlib.metadata import PackageNotFoundError, version

from .appium import (
    build_appium_options,
    create_appium_driver,
    get_connection_target_from_driver,
    get_device_config_for_driver,
)
from .allocation import AllocatedDevice, hydrate_allocated_device, hydrate_allocated_device_from_driver
from .client import (
    GRID_URL,
    GRIDFLEET_API_URL,
    GridFleetClient,
    HeartbeatThread,
    NoClaimableDevicesError,
    register_run_cleanup,
)
from .sessions import build_error_session_payload

try:
    __version__ = version("gridfleet-testkit")
except PackageNotFoundError:
    __version__ = "0.2.1"

__all__ = [
    "AllocatedDevice",
    "GRIDFLEET_API_URL",
    "GRID_URL",
    "GridFleetClient",
    "HeartbeatThread",
    "NoClaimableDevicesError",
    "__version__",
    "build_appium_options",
    "build_error_session_payload",
    "create_appium_driver",
    "get_connection_target_from_driver",
    "get_device_config_for_driver",
    "hydrate_allocated_device",
    "hydrate_allocated_device_from_driver",
    "register_run_cleanup",
]
