"""Supported pytest plugin surface for GridFleet Appium tests."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from .appium import (
    build_appium_options,
    get_device_config_for_driver,
    get_device_test_data_for_driver,
)
from .client import GRID_URL, GridFleetClient
from .sessions import build_error_session_payload

if TYPE_CHECKING:
    from collections.abc import Generator


def _normalize_usage_error_message(message: str) -> str:
    if message.startswith("Appium options require"):
        return (
            "appium_driver requires pack_id + platform_id, platform_id with an unambiguous catalog match, "
            "or an explicit 'platformName' capability."
        )
    return message


def _build_driver_options(request: pytest.FixtureRequest, catalog_client: GridFleetClient | None = None) -> Any:
    params = getattr(request, "param", {})
    capabilities = {key: value for key, value in params.items() if key not in {"pack_id", "platform_id"}}
    try:
        return build_appium_options(
            pack_id=params.get("pack_id"),
            platform_id=params.get("platform_id"),
            capabilities=capabilities,
            test_name=request.node.name,
            catalog_client=catalog_client or GridFleetClient(),
        )
    except ValueError as exc:
        raise pytest.UsageError(_normalize_usage_error_message(str(exc))) from exc


@pytest.fixture
def appium_driver(request: pytest.FixtureRequest, gridfleet_client: GridFleetClient) -> Generator[Any, None, None]:
    """
    Create an Appium Remote driver through the Selenium Grid.

    Parametrize with a dict of pack/catalog selection plus capabilities:
        @pytest.mark.parametrize(
            "appium_driver",
            [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}],
            indirect=True,
        )
    """
    options = _build_driver_options(request, gridfleet_client)
    # appium is an optional dep (extra "appium"); imported lazily so consumers
    # without the extra can still use the rest of testkit.
    from appium import webdriver  # noqa: PLC0415

    try:
        driver = webdriver.Remote(GRID_URL, options=options)
    except Exception as exc:
        # Driver creation failed before a Grid session was established (e.g.
        # SessionNotCreatedException). Register a device-less error session so the
        # failure is visible in the Dashboard Sessions view.
        params = getattr(request, "param", {})
        pack_id = params.get("pack_id")
        platform_id = params.get("platform_id")
        payload = build_error_session_payload(
            session_id=f"error-{uuid.uuid4()}",
            test_name=request.node.name,
            options=options,
            exc=exc,
            pack_id=pack_id if isinstance(pack_id, str) and pack_id else None,
            platform_id=platform_id if isinstance(platform_id, str) and platform_id else None,
        )
        gridfleet_client.register_session(**payload, suppress_errors=True)
        raise
    session_id = driver.session_id
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Created Appium driver did not expose a session ID")
    gridfleet_client.register_session_from_driver(driver, test_name=request.node.name, suppress_errors=True)

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
def pytest_runtest_makereport(item: pytest.Item) -> Generator[Any, None, None]:
    """Store the test outcome on the item for fixture teardown reporting."""
    outcome: Any = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


@pytest.fixture(scope="session")
def gridfleet_client() -> GridFleetClient:
    return GridFleetClient()


@pytest.fixture
def device_config(appium_driver: Any, gridfleet_client: GridFleetClient) -> dict[str, Any]:
    """
    Fetch device config after the Grid assigns a runtime connection target.

    Usage:
        def test_login(appium_driver, device_config):
            username = device_config["app_username"]
    """
    try:
        return get_device_config_for_driver(appium_driver, gridfleet_client=gridfleet_client)
    except ValueError as exc:
        pytest.skip("Could not determine device connection target from session capabilities")
        raise RuntimeError("unreachable: pytest.skip did not raise") from exc


@pytest.fixture
def device_test_data(appium_driver: Any, gridfleet_client: GridFleetClient) -> dict[str, Any]:
    """Fetch operator-attached test_data after the Grid assigns a runtime connection target."""
    try:
        return get_device_test_data_for_driver(appium_driver, gridfleet_client=gridfleet_client)
    except ValueError as exc:
        pytest.skip("Could not determine device connection target from session capabilities")
        raise RuntimeError("unreachable: pytest.skip did not raise") from exc
