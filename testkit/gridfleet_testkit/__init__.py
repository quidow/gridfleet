"""Supported Python integration helpers for GridFleet.

Environment variables read by the client:

- GRID_URL: Selenium Grid URL used by Appium helper defaults.
- GRIDFLEET_API_URL: GridFleet manager API base URL.
- GRIDFLEET_TESTKIT_USERNAME: optional Basic auth username.
- GRIDFLEET_TESTKIT_PASSWORD: optional Basic auth password.

Recipe-local run-state sharing variables are intentionally not exported from
this package because run-state sharing is consumer policy.
"""

from importlib.metadata import PackageNotFoundError, version

from .allocation import AllocatedDevice, hydrate_allocated_device, hydrate_allocated_device_from_driver
from .appium import (
    build_appium_options,
    create_appium_driver,
    get_connection_target_from_driver,
    get_device_config_for_driver,
)
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
    __version__ = "0.3.0"

__all__ = [
    "GRIDFLEET_API_URL",
    "GRID_URL",
    "AllocatedDevice",
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
