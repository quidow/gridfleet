"""Driver-pack catalog resolution: platform metadata for Appium options."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, TypedDict, cast

from .client import GridFleetClient

if TYPE_CHECKING:
    from collections.abc import Callable

    from .types import JsonObject


class CatalogPlatform(TypedDict, total=False):
    id: str
    appium_platform_name: str
    automation_name: str


class CatalogPack(TypedDict, total=False):
    id: str
    state: str
    platforms: list[CatalogPlatform]


class DriverPackCatalog(TypedDict, total=False):
    packs: list[CatalogPack]


def _catalog_payload(catalog_client: object | None) -> JsonObject:
    if catalog_client is None:
        catalog_client = GridFleetClient()
    catalog_getter = getattr(catalog_client, "get_driver_pack_catalog", None)
    if callable(catalog_getter):
        payload = catalog_getter()
    elif callable(catalog_client):
        payload = cast("Callable[[], object]", catalog_client)()
    else:
        payload = catalog_client
    if isinstance(payload, dict):
        return cast("JsonObject", payload)
    if isinstance(payload, list):
        return cast("JsonObject", {"packs": payload})
    raise ValueError("Driver pack catalog client returned an invalid payload")


def _enabled_platform_matches(catalog: JsonObject, platform_id: str) -> list[tuple[CatalogPack, CatalogPlatform]]:
    raw_packs = catalog.get("packs")
    if not isinstance(raw_packs, list):
        raise ValueError("Driver pack catalog payload must include a packs list")
    matches: list[tuple[CatalogPack, CatalogPlatform]] = []
    for raw_pack in raw_packs:
        if not isinstance(raw_pack, dict) or raw_pack.get("state") != "enabled":
            continue
        pack = cast("CatalogPack", raw_pack)
        raw_platforms = pack.get("platforms")
        if not isinstance(raw_platforms, list):
            continue
        for raw_platform in raw_platforms:
            if isinstance(raw_platform, dict) and raw_platform.get("id") == platform_id:
                matches.append((pack, raw_platform))
    return matches


def _required_platform_string(platform: CatalogPlatform, key: str) -> str:
    value = platform.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"Driver pack platform is missing {key}")
    return value


def _resolve_pack_platform(
    *,
    pack_id: str | None,
    platform_id: str | None,
    catalog_client: object | None,
) -> tuple[str, CatalogPlatform]:
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
