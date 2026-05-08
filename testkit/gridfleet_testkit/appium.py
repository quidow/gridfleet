"""Public Appium driver helpers for GridFleet integrations."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

from .client import GridFleetClient, _default_grid_url


def _catalog_payload(catalog_client: Any | None) -> dict[str, Any]:
    if catalog_client is None:
        catalog_client = GridFleetClient()
    if hasattr(catalog_client, "get_driver_pack_catalog"):
        payload = catalog_client.get_driver_pack_catalog()
    elif callable(catalog_client):
        payload = catalog_client()
    else:
        payload = catalog_client
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list):
        return {"packs": payload}
    raise ValueError("Driver pack catalog client returned an invalid payload")


def _enabled_platform_matches(catalog: dict[str, Any], platform_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    packs = catalog.get("packs")
    if not isinstance(packs, list):
        raise ValueError("Driver pack catalog payload must include a packs list")
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for pack in packs:
        if not isinstance(pack, dict) or pack.get("state") != "enabled":
            continue
        platforms = pack.get("platforms")
        if not isinstance(platforms, list):
            continue
        for platform in platforms:
            if isinstance(platform, dict) and platform.get("id") == platform_id:
                matches.append((pack, platform))
    return matches


def _resolve_pack_platform(
    *,
    pack_id: str | None,
    platform_id: str | None,
    catalog_client: Any | None,
) -> tuple[str, dict[str, Any]]:
    resolved_pack_id = pack_id or os.getenv("GRIDFLEET_TESTKIT_PACK_ID")
    resolved_platform_id = platform_id or os.getenv("GRIDFLEET_TESTKIT_PLATFORM_ID")
    if not resolved_platform_id:
        raise ValueError(
            "Appium options require pack_id + platform_id, platform_id with an unambiguous catalog match, "
            "or an explicit raw platformName capability."
        )

    catalog = _catalog_payload(catalog_client)
    matches = _enabled_platform_matches(catalog, resolved_platform_id)
    if resolved_pack_id:
        for pack, platform in matches:
            if pack.get("id") == resolved_pack_id:
                return resolved_pack_id, platform
        raise ValueError(f"Enabled driver pack platform {resolved_pack_id}:{resolved_platform_id} was not found")

    if len(matches) == 1:
        pack, platform = matches[0]
        pack_id_value = pack.get("id")
        if not isinstance(pack_id_value, str) or not pack_id_value:
            raise ValueError("Driver pack catalog entry is missing id")
        return pack_id_value, platform
    if len(matches) > 1:
        raise ValueError(f"Multiple enabled driver packs provide platform_id {resolved_platform_id!r}; pass pack_id")
    raise ValueError(f"Enabled driver pack platform for platform_id {resolved_platform_id!r} was not found")


def _required_platform_string(platform: dict[str, Any], key: str) -> str:
    value = platform.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Driver pack platform is missing {key}")
    return value


def build_appium_options(
    *,
    pack_id: str | None = None,
    platform_id: str | None = None,
    capabilities: Mapping[str, Any] | None = None,
    test_name: str | None = None,
    catalog_client: Any | None = None,
) -> Any:
    """Build Appium options from driver-pack catalog platform metadata."""
    # appium is an optional dep (extra "appium"); imported lazily so consumers
    # without the extra can still use the rest of testkit.
    from appium.options.common import AppiumOptions  # noqa: PLC0415

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


def create_appium_driver(
    *,
    pack_id: str | None = None,
    platform_id: str | None = None,
    capabilities: Mapping[str, Any] | None = None,
    test_name: str | None = None,
    grid_url: str | None = None,
    catalog_client: Any | None = None,
) -> Any:
    """Create an Appium remote driver through Selenium Grid."""
    # appium is an optional dep (extra "appium"); imported lazily so consumers
    # without the extra can still use the rest of testkit.
    from appium import webdriver  # noqa: PLC0415

    options = build_appium_options(
        pack_id=pack_id,
        platform_id=platform_id,
        capabilities=capabilities,
        test_name=test_name,
        catalog_client=catalog_client,
    )
    return webdriver.Remote(grid_url or _default_grid_url(), options=options)


def get_connection_target_from_driver(driver: Any) -> str:
    """Return the runtime connection target from a live Appium driver."""
    capabilities = driver.capabilities
    connection_target = capabilities.get("appium:udid")
    if not isinstance(connection_target, str) or not connection_target:
        raise ValueError("Could not determine device connection target from session capabilities")
    return connection_target


def get_device_config_for_driver(
    driver: Any,
    *,
    gridfleet_client: GridFleetClient | None = None,
) -> dict[str, Any]:
    """Fetch device config for a live Appium driver using its runtime connection target."""
    client = gridfleet_client or GridFleetClient()
    return client.get_device_config(get_connection_target_from_driver(driver))


def get_device_test_data_for_driver(
    driver: Any,
    *,
    gridfleet_client: GridFleetClient | None = None,
) -> dict[str, Any]:
    """Fetch operator-attached test_data for a live Appium driver session."""
    client = gridfleet_client or GridFleetClient()
    connection_target = get_connection_target_from_driver(driver)
    device_id = client.resolve_device_id_by_connection_target(connection_target)
    return client.get_device_test_data(device_id)
