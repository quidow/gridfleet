"""Supported Python integration helpers for GridFleet.

Environment variables read by the client:

- GRID_URL: WebDriver router URL used by Appium helper defaults.
- GRIDFLEET_API_URL: GridFleet manager API base URL.
- GRIDFLEET_TESTKIT_USERNAME: optional Basic auth username.
- GRIDFLEET_TESTKIT_PASSWORD: optional Basic auth password.

The resolved URLs are also available programmatically via ``grid_url()`` and
``api_url()`` exported from this package.

Recipe-local run-state sharing variables are intentionally not exported from
this package because run-state sharing is consumer policy.
"""

from importlib.metadata import PackageNotFoundError, version

from .allocation import (
    AllocatedDevice,
    UnavailableInclude,
    hydrate_allocated_device,
    hydrate_allocated_device_from_driver,
)
from .client import (
    GridFleetClient,
)
from .config import api_url, grid_url, run_grid_url
from .driver import (
    build_appium_options,
    create_appium_driver,
)
from .errors import ReserveCapabilitiesUnsupportedError, UnknownIncludeError
from .run_lifecycle import HeartbeatThread, register_run_cleanup
from .session import (
    get_device_id_from_driver,
    get_device_test_data_for_driver,
    resolve_device_handle_from_driver,
)
from .types import (
    CooldownEscalatedResult,
    CooldownResult,
    CooldownSetResult,
)

try:
    __version__ = version("gridfleet-testkit")
except PackageNotFoundError:
    __version__ = "0.12.0"

__all__ = [
    "AllocatedDevice",
    "CooldownEscalatedResult",
    "CooldownResult",
    "CooldownSetResult",
    "GridFleetClient",
    "HeartbeatThread",
    "ReserveCapabilitiesUnsupportedError",
    "UnavailableInclude",
    "UnknownIncludeError",
    "__version__",
    "api_url",
    "build_appium_options",
    "create_appium_driver",
    "get_device_id_from_driver",
    "get_device_test_data_for_driver",
    "grid_url",
    "hydrate_allocated_device",
    "hydrate_allocated_device_from_driver",
    "register_run_cleanup",
    "resolve_device_handle_from_driver",
    "run_grid_url",
]
