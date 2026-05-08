"""Supported Python integration helpers for GridFleet.

`device_config` values returned by the manager are verbatim; the testkit no
longer distinguishes between masked and revealed payloads. Code that wants
the live Appium-side config can use either inline `config` from
`claim_device(include=("config",))` or `client.get_device_config(connection_target)`.

Environment variables read by the client:

- GRID_URL: Selenium Grid URL used by Appium helper defaults.
- GRIDFLEET_API_URL: GridFleet manager API base URL.
- GRIDFLEET_TESTKIT_USERNAME: optional Basic auth username.
- GRIDFLEET_TESTKIT_PASSWORD: optional Basic auth password.

Recipe-local run-state sharing variables are intentionally not exported from
this package because run-state sharing is consumer policy.
"""

from importlib.metadata import PackageNotFoundError, version

from . import client as _client_mod
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
    CooldownResult,
    GridFleetClient,
    HeartbeatThread,
    NoClaimableDevicesError,
    ReserveCapabilitiesUnsupportedError,
    UnknownIncludeError,
    register_run_cleanup,
)
from .sessions import build_error_session_payload

try:
    __version__ = version("gridfleet-testkit")
except PackageNotFoundError:
    __version__ = "0.4.0"

__all__ = [
    "GRIDFLEET_API_URL",
    "GRID_URL",
    "AllocatedDevice",
    "CooldownResult",
    "GridFleetClient",
    "HeartbeatThread",
    "NoClaimableDevicesError",
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
]


def __getattr__(name: str) -> object:
    if name in {"GRID_URL", "GRIDFLEET_API_URL"}:
        return getattr(_client_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
