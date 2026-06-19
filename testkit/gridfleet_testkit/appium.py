"""Public Appium driver helpers for GridFleet integrations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from appium import webdriver
from appium.options.common import AppiumOptions

from . import config
from .catalog import _required_platform_string, _resolve_pack_platform

if TYPE_CHECKING:
    from collections.abc import Mapping

    from appium.webdriver.client_config import AppiumClientConfig
    from appium.webdriver.webdriver import WebDriver


def build_appium_options(
    *,
    pack_id: str | None = None,
    platform_id: str | None = None,
    capabilities: Mapping[str, object] | None = None,
    test_name: str | None = None,
    catalog_client: object | None = None,
) -> AppiumOptions:
    """Build Appium options from driver-pack catalog platform metadata."""
    params = dict(capabilities or {})
    explicit_platform_name = params.get("platformName")
    if explicit_platform_name is not None and (pack_id is not None or platform_id is not None):
        raise ValueError("Use either pack_id/platform_id or the raw platformName capability, not both.")

    options = AppiumOptions()
    if explicit_platform_name is None:
        _pack_id, platform_data = _resolve_pack_platform(
            pack_id=pack_id,
            platform_id=platform_id,
            catalog_client=catalog_client,
        )
        options.platform_name = _required_platform_string(platform_data, "appium_platform_name")
        options.set_capability("appium:automationName", _required_platform_string(platform_data, "automation_name"))
        options.set_capability("appium:platform", _required_platform_string(platform_data, "id"))

    for key, value in params.items():
        options.set_capability(key, value)

    if test_name is not None:
        options.set_capability("gridfleet:testName", test_name)
    return options


def _remote_with_owned_endpoint(
    grid_endpoint: str,
    options: AppiumOptions,
    client_config: AppiumClientConfig | None,
) -> WebDriver:
    """Build the Appium driver with the testkit owning the connection endpoint.

    Appium's ``webdriver.Remote`` ignores the URL argument when a ``client_config``
    is supplied, so the resolved grid endpoint is written onto the config in place.
    """
    if client_config is not None:
        client_config.remote_server_addr = grid_endpoint
    return webdriver.Remote(grid_endpoint, options=options, client_config=client_config)


def create_appium_driver(
    *,
    pack_id: str | None = None,
    platform_id: str | None = None,
    capabilities: Mapping[str, object] | None = None,
    test_name: str | None = None,
    grid_url: str | None = None,
    catalog_client: object | None = None,
    client_config: AppiumClientConfig | None = None,
) -> WebDriver:
    """Create an Appium remote driver through the WebDriver router.

    ``client_config`` lets callers tune the HTTP transport (connection retries,
    timeouts, proxy, TLS, headers). The testkit still owns the endpoint: any
    ``remote_server_addr`` set on the config is overwritten with the resolved
    grid URL.
    """
    options = build_appium_options(
        pack_id=pack_id,
        platform_id=platform_id,
        capabilities=capabilities,
        test_name=test_name,
        catalog_client=catalog_client,
    )
    return _remote_with_owned_endpoint(config.resolve_grid_url(grid_url), options, client_config)
