"""Supported Python integration helpers for GridFleet.

`device_config` values returned by the manager are verbatim; the testkit no
longer distinguishes between masked and revealed payloads. Code that wants
the live Appium-side config can use `client.get_device_config(connection_target)`.

Environment variables read by the client:

- GRID_URL: Selenium Grid URL used by Appium helper defaults.
- GRIDFLEET_API_URL: GridFleet manager API base URL.
- GRIDFLEET_TESTKIT_USERNAME: optional Basic auth username.
- GRIDFLEET_TESTKIT_PASSWORD: optional Basic auth password.

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
from .appium import (
    build_appium_options,
    create_appium_driver,
    get_connection_target_from_driver,
    get_device_config_for_driver,
    get_device_test_data_for_driver,
)
from .client import (
    CooldownEscalatedResult,
    CooldownResult,
    CooldownSetResult,
    GridFleetClient,
    HeartbeatThread,
    ReserveCapabilitiesUnsupportedError,
    UnknownIncludeError,
    _default_api_url,
    _default_grid_url,
    register_run_cleanup,
)
from .sessions import build_error_session_payload, resolve_device_handle_from_driver

try:
    __version__ = version("gridfleet-testkit")
except PackageNotFoundError:
    __version__ = "0.7.0"

__all__ = [
    "GRIDFLEET_API_URL",
    "GRID_URL",
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
    "build_appium_options",
    "build_error_session_payload",
    "create_appium_driver",
    "get_connection_target_from_driver",
    "get_device_config_for_driver",
    "get_device_test_data_for_driver",
    "hydrate_allocated_device",
    "hydrate_allocated_device_from_driver",
    "register_run_cleanup",
    "resolve_device_handle_from_driver",
]


def __getattr__(name: str) -> str:
    if name == "GRID_URL":
        return _default_grid_url()
    if name == "GRIDFLEET_API_URL":
        return _default_api_url()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
