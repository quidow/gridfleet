from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.driver_pack import DriverPack, DriverPackRelease
from app.services.config_service import MASK_VALUE, SENSITIVE_PATTERNS
from app.services.pack_release_ordering import selected_release

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device


def _mask_keys(config: dict[str, Any], sensitive_keys: set[str]) -> dict[str, Any]:
    masked: dict[str, Any] = {}
    for key, value in config.items():
        if isinstance(value, dict):
            masked[key] = _mask_keys(value, sensitive_keys)
        elif key in sensitive_keys or SENSITIVE_PATTERNS.search(key):
            masked[key] = MASK_VALUE
        else:
            masked[key] = copy.deepcopy(value)
    return masked


async def sensitive_config_keys_for_device(session: AsyncSession, device: Device) -> set[str]:
    pack = await session.scalar(
        select(DriverPack)
        .where(DriverPack.id == device.pack_id)
        .options(selectinload(DriverPack.releases).selectinload(DriverPackRelease.platforms))
    )
    if pack is None:
        return set()

    release = selected_release(pack.releases, pack.current_release)
    platform = (
        next((row for row in release.platforms if row.manifest_platform_id == device.platform_id), None)
        if release is not None
        else None
    )
    if platform is None:
        return set()
    keys: set[str] = set()
    for field in platform.data.get("device_fields_schema", []):
        if isinstance(field, dict) and field.get("sensitive") is True:
            field_id = field.get("id")
            capability_name = field.get("capability_name")
            if isinstance(field_id, str):
                keys.add(field_id)
            if isinstance(capability_name, str):
                keys.add(capability_name)
    return keys


async def load_sensitive_config_key_map(
    session: AsyncSession,
    devices: Iterable[Device],
) -> dict[tuple[str, str], set[str]]:
    key_map: dict[tuple[str, str], set[str]] = {}
    for device in devices:
        pair = (device.pack_id, device.platform_id)
        if pair not in key_map:
            key_map[pair] = await sensitive_config_keys_for_device(session, device)
    return key_map


async def mask_device_config(
    session: AsyncSession,
    device: Device,
    config: dict[str, Any] | None,
    *,
    reveal: bool = False,
    sensitive_key_map: Mapping[tuple[str, str], set[str]] | None = None,
) -> dict[str, Any]:
    source = copy.deepcopy(config or {})
    if reveal:
        return source
    sensitive_keys = (
        sensitive_key_map.get((device.pack_id, device.platform_id), set())
        if sensitive_key_map is not None
        else await sensitive_config_keys_for_device(session, device)
    )
    return _mask_keys(source, sensitive_keys)
