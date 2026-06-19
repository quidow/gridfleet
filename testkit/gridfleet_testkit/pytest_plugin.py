"""Supported pytest plugin surface for GridFleet Appium tests."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from . import config
from .client import GridFleetClient
from .driver import (
    _remote_with_owned_endpoint,
    build_appium_options,
)
from .session import get_device_test_data_for_driver, resolve_device_handle_from_driver

if TYPE_CHECKING:
    from collections.abc import Generator

    from appium.options.common import AppiumOptions
    from appium.webdriver.client_config import AppiumClientConfig
    from appium.webdriver.webdriver import WebDriver
    from pluggy import Result

    from .types import JsonObject


def _normalize_usage_error_message(message: str) -> str:
    if message.startswith("Appium options require"):
        return (
            "appium_driver requires pack_id + platform_id, platform_id with an unambiguous catalog match, "
            "or an explicit 'platformName' capability."
        )
    return message


def _request_params(request: pytest.FixtureRequest) -> dict[str, object]:
    params = getattr(request, "param", {})
    return cast("dict[str, object]", params) if isinstance(params, dict) else {}


def _build_driver_options(
    request: pytest.FixtureRequest,
    catalog_client: GridFleetClient | None = None,
) -> AppiumOptions:
    params = _request_params(request)
    capabilities = {key: value for key, value in params.items() if key not in {"pack_id", "platform_id"}}
    pack_id = params.get("pack_id")
    platform_id = params.get("platform_id")
    try:
        return build_appium_options(
            pack_id=pack_id if isinstance(pack_id, str) else None,
            platform_id=platform_id if isinstance(platform_id, str) else None,
            capabilities=capabilities,
            test_name=request.node.name,
            catalog_client=catalog_client or GridFleetClient(),
        )
    except ValueError as exc:
        raise pytest.UsageError(_normalize_usage_error_message(str(exc))) from exc


@pytest.fixture
def gridfleet_client_config() -> AppiumClientConfig | None:
    """Override in your conftest to tune the Appium HTTP transport (connection
    retries, timeouts, proxy, TLS) for every ``appium_driver`` session. The
    testkit still owns the endpoint, so any ``remote_server_addr`` is overwritten
    with the resolved grid URL.
    """
    return None


@pytest.fixture
def appium_driver(
    request: pytest.FixtureRequest,
    gridfleet_client: GridFleetClient,
    gridfleet_client_config: AppiumClientConfig | None,
) -> Generator[WebDriver, None, None]:
    """
    Create an Appium Remote driver through the WebDriver router.

    Parametrize with a dict of pack/catalog selection plus capabilities:
        @pytest.mark.parametrize(
            "appium_driver",
            [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}],
            indirect=True,
        )

    Override the ``gridfleet_client_config`` fixture to tune the HTTP transport.
    """
    options = _build_driver_options(request, gridfleet_client)

    driver = _remote_with_owned_endpoint(config.resolve_grid_url(None), options, gridfleet_client_config)
    session_id = driver.session_id
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Created Appium driver did not expose a session ID")

    yield driver

    status: str | None = None
    if hasattr(request.node, "rep_call"):
        if request.node.rep_call.passed:
            status = "passed"
        elif request.node.rep_call.failed:
            status = "failed"
        else:
            status = "error"

    try:
        driver.quit()
    finally:
        if status is not None:
            gridfleet_client.update_session_status(session_id, status, suppress_errors=True)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> Generator[None, Result[pytest.TestReport], None]:
    """Store the test outcome on the item for fixture teardown reporting."""
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(scope="session")
def gridfleet_client() -> GridFleetClient:
    return GridFleetClient()


@pytest.fixture
def device_test_data(appium_driver: WebDriver, gridfleet_client: GridFleetClient) -> JsonObject:
    """Fetch operator-attached test_data for the device the live session landed on.

    The device is resolved from the ``appium:gridfleet:deviceId`` session capability.
    """
    try:
        return get_device_test_data_for_driver(appium_driver, gridfleet_client=gridfleet_client)
    except ValueError as exc:
        pytest.skip(str(exc))
        raise RuntimeError("unreachable: pytest.skip did not raise") from exc


@pytest.fixture
def device_handle(appium_driver: WebDriver, gridfleet_client: GridFleetClient) -> JsonObject:
    """Fetch the canonical manager device row for the device the live session landed on.

    The device is resolved from the ``appium:gridfleet:deviceId`` session capability.
    """
    try:
        return resolve_device_handle_from_driver(appium_driver, client=gridfleet_client)
    except (RuntimeError, ValueError) as exc:
        pytest.skip(str(exc))
        raise RuntimeError("unreachable: pytest.skip did not raise") from exc


def _gridfleet_worker_id(request: pytest.FixtureRequest) -> str:
    """Return pytest-xdist worker id, or controller for non-worker processes."""
    workerinput = getattr(request.config, "workerinput", None)
    if isinstance(workerinput, dict):
        value = workerinput.get("workerid")
        if isinstance(value, str) and value:
            return value
    return "controller"


gridfleet_worker_id = pytest.fixture(_gridfleet_worker_id)
