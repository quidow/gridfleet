import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device
from app.devices.services import readiness as device_readiness
from app.events import queue_event_for_session
from app.settings.models import ConfigAuditLog


def _filter_keys(config: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Return only the requested top-level keys from the config."""
    return {k: v for k, v in config.items() if k in keys}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep merge override into base, returning a new dict."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# --- Device config operations ---


async def get_device_config(
    db: AsyncSession,
    device: Device,
    keys: list[str] | None = None,
) -> dict[str, Any]:
    config = device.device_config or {}
    if keys:
        config = _filter_keys(config, keys)
    return copy.deepcopy(config)


async def replace_device_config(
    db: AsyncSession,
    device: Device,
    new_config: dict[str, Any],
    changed_by: str | None = None,
) -> dict[str, Any]:
    previous = device.device_config or {}
    if device_readiness.payload_requires_reverification(device, {"device_config": new_config}):
        device.verified_at = None
    device.device_config = new_config

    log_entry = ConfigAuditLog(
        device_id=device.id,
        previous_config=copy.deepcopy(previous),
        new_config=copy.deepcopy(new_config),
        changed_by=changed_by,
    )
    db.add(log_entry)
    queue_event_for_session(
        db,
        "config.updated",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "changed_by": changed_by,
        },
    )
    await db.commit()
    await db.refresh(device)
    return copy.deepcopy(device.device_config or {})


async def merge_device_config(
    db: AsyncSession,
    device: Device,
    partial_config: dict[str, Any],
    changed_by: str | None = None,
) -> dict[str, Any]:
    previous = device.device_config or {}
    merged = _deep_merge(previous, partial_config)
    if device_readiness.payload_requires_reverification(device, {"device_config": merged}):
        device.verified_at = None
    device.device_config = merged

    log_entry = ConfigAuditLog(
        device_id=device.id,
        previous_config=copy.deepcopy(previous),
        new_config=copy.deepcopy(merged),
        changed_by=changed_by,
    )
    db.add(log_entry)
    queue_event_for_session(
        db,
        "config.updated",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "changed_by": changed_by,
        },
    )
    await db.commit()
    await db.refresh(device)
    return copy.deepcopy(device.device_config or {})


async def get_config_history(
    db: AsyncSession,
    device_id: uuid.UUID,
    limit: int = 50,
) -> list[ConfigAuditLog]:
    stmt = (
        select(ConfigAuditLog)
        .where(ConfigAuditLog.device_id == device_id)
        .order_by(ConfigAuditLog.changed_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
