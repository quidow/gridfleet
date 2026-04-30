"""Supported pytest plugin surface for GridFleet Appium tests."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from .appium import (
    build_appium_options,
    get_device_config_for_driver,
)
from .client import GRID_URL, GRIDFLEET_API_URL, GridFleetClient

if TYPE_CHECKING:
    from collections.abc import Generator

logger = logging.getLogger("gridfleet_testkit")
KNOWN_DEVICE_TYPES = {"real_device", "emulator", "simulator"}
KNOWN_CONNECTION_TYPES = {"usb", "network"}


def _normalize_usage_error_message(message: str) -> str:
    if message.startswith("Appium options require"):
        return (
            "appium_driver requires pack_id + platform_id, platform_id with an unambiguous catalog match, "
            "or an explicit 'platformName' capability."
        )
    return message


def _report_session_status(session_id: str, status: str) -> None:
    """Report final session status to the GridFleet API."""
    try:
        resp = httpx.patch(
            f"{GRIDFLEET_API_URL}/sessions/{session_id}/status",
            json={"status": status},
            timeout=5,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Failed to report session status to GridFleet: %s", exc)


def _register_session(driver: Any, test_name: str) -> None:
    """Register a newly-created Grid session with the GridFleet API."""
    capabilities = getattr(driver, "capabilities", {})
    if not isinstance(capabilities, dict):
        capabilities = {}

    payload: dict[str, Any] = {
        "session_id": driver.session_id,
        "test_name": test_name,
    }
    device_id = capabilities.get("appium:gridfleet:deviceId") or capabilities.get("gridfleet:deviceId")
    if isinstance(device_id, str) and device_id:
        payload["device_id"] = device_id

    connection_target = capabilities.get("appium:udid") or capabilities.get("appium:deviceName")
    if isinstance(connection_target, str) and connection_target:
        payload["connection_target"] = connection_target

    try:
        resp = httpx.post(
            f"{GRIDFLEET_API_URL}/sessions",
            json=payload,
            timeout=5,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Failed to register session with GridFleet: %s", exc)


def _register_error_session(payload: dict[str, Any]) -> None:
    """Register a device-less error session with the GridFleet API.

    Used when driver creation fails before a Grid session is established so
    the failure is still visible in the Dashboard Sessions view.
    """
    try:
        resp = httpx.post(
            f"{GRIDFLEET_API_URL}/sessions",
            json=payload,
            timeout=5,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Failed to register error session with GridFleet: %s", exc)


def _build_driver_options(request: pytest.FixtureRequest) -> Any:
    params = getattr(request, "param", {})
    capabilities = {key: value for key, value in params.items() if key not in {"pack_id", "platform_id"}}
    try:
        return build_appium_options(
            pack_id=params.get("pack_id"),
            platform_id=params.get("platform_id"),
            capabilities=capabilities,
            test_name=request.node.name,
            catalog_client=GridFleetClient(),
        )
    except ValueError as exc:
        raise pytest.UsageError(_normalize_usage_error_message(str(exc))) from exc


def _raw_attempted_capabilities(options: Any) -> dict[str, Any]:
    capabilities = getattr(options, "capabilities", {})
    raw_capabilities = dict(capabilities) if isinstance(capabilities, dict) else {}
    platform_name = getattr(options, "platform_name", None)
    if isinstance(platform_name, str) and platform_name:
        raw_capabilities.setdefault("platformName", platform_name)
    return raw_capabilities


def _infer_requested_platform_id(params: dict[str, Any], raw_capabilities: dict[str, Any]) -> str | None:
    platform_id = params.get("platform_id")
    if isinstance(platform_id, str) and platform_id:
        return platform_id
    platform_hint = raw_capabilities.get("appium:platform")
    return platform_hint if isinstance(platform_hint, str) and platform_hint else None


def _read_enum_capability(raw_capabilities: dict[str, Any], *keys: str, allowed: set[str]) -> str | None:
    for key in keys:
        value = raw_capabilities.get(key)
        if isinstance(value, str) and value in allowed:
            return value
    return None


def _build_error_session_payload(
    *,
    request: pytest.FixtureRequest,
    options: Any,
    exc: Exception,
    session_id: str,
) -> dict[str, Any]:
    params = getattr(request, "param", {})
    raw_capabilities = _raw_attempted_capabilities(options)
    payload: dict[str, Any] = {
        "session_id": session_id,
        "test_name": request.node.name,
        "status": "error",
        "requested_platform_id": _infer_requested_platform_id(params, raw_capabilities),
        "requested_device_type": _read_enum_capability(
            raw_capabilities,
            "appium:device_type",
            "device_type",
            allowed=KNOWN_DEVICE_TYPES,
        ),
        "requested_connection_type": _read_enum_capability(
            raw_capabilities,
            "appium:connection_type",
            "connection_type",
            allowed=KNOWN_CONNECTION_TYPES,
        ),
        "requested_capabilities": raw_capabilities,
        "error_type": type(exc).__name__,
        "error_message": str(exc),
    }
    return payload


@pytest.fixture
def appium_driver(request: pytest.FixtureRequest) -> Generator[Any, None, None]:
    """
    Create an Appium Remote driver through the Selenium Grid.

    Parametrize with a dict of pack/catalog selection plus capabilities:
        @pytest.mark.parametrize(
            "appium_driver",
            [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile"}],
            indirect=True,
        )
    """
    try:
        options = _build_driver_options(request)
    except ValueError as exc:
        raise pytest.UsageError(_normalize_usage_error_message(str(exc))) from exc
    from appium import webdriver

    try:
        driver = webdriver.Remote(GRID_URL, options=options)
    except Exception as exc:
        # Driver creation failed before a Grid session was established (e.g.
        # SessionNotCreatedException). Register a device-less error session so the
        # failure is visible in the Dashboard Sessions view.
        synthetic_id = f"error-{uuid.uuid4()}"
        _register_error_session(
            _build_error_session_payload(request=request, options=options, exc=exc, session_id=synthetic_id)
        )
        raise
    session_id = driver.session_id
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("Created Appium driver did not expose a session ID")
    _register_session(driver, request.node.name)

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
            _report_session_status(session_id, status)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Generator[Any, None, None]:
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
    except ValueError:
        pytest.skip("Could not determine device connection target from session capabilities")
