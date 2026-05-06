import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config_audit_log import ConfigAuditLog
from app.models.device import Device
from app.services import device_readiness
from app.services.device_config_masking import mask_device_config, preserve_masked_device_config_values
from app.services.device_config_masking_primitives import MASK_VALUE as _MASK_VALUE
from app.services.device_config_masking_primitives import deep_merge
from app.services.event_bus import queue_event_for_session

MASK_VALUE = _MASK_VALUE


def _filter_keys(config: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    """Return only the requested top-level keys from the config."""
    return {k: v for k, v in config.items() if k in keys}


# --- Device config operations ---


async def get_device_config(
    db: AsyncSession,
    device: Device,
    keys: list[str] | None = None,
    reveal: bool = False,
) -> dict[str, Any]:
    config = device.device_config or {}
    if keys:
        config = _filter_keys(config, keys)
    return await mask_device_config(db, device, config, reveal=reveal)


async def replace_device_config(
    db: AsyncSession,
    device: Device,
    new_config: dict[str, Any],
    changed_by: str | None = None,
) -> dict[str, Any]:
    previous = device.device_config or {}
    new_config = await preserve_masked_device_config_values(
        db,
        device,
        existing_config=previous,
        next_config=new_config,
    )
    if device_readiness.payload_requires_reverification(device, {"device_config": new_config}):
        device.verified_at = None
    device.device_config = new_config

    # Audit log
    log_entry = ConfigAuditLog(
        device_id=device.id,
        previous_config=await mask_device_config(db, device, previous),
        new_config=await mask_device_config(db, device, new_config),
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
    return await mask_device_config(db, device, device.device_config)


async def merge_device_config(
    db: AsyncSession,
    device: Device,
    partial_config: dict[str, Any],
    changed_by: str | None = None,
) -> dict[str, Any]:
    previous = device.device_config or {}
    merged = deep_merge(previous, partial_config)
    merged = await preserve_masked_device_config_values(
        db,
        device,
        existing_config=previous,
        next_config=merged,
    )
    if device_readiness.payload_requires_reverification(device, {"device_config": merged}):
        device.verified_at = None
    device.device_config = merged

    log_entry = ConfigAuditLog(
        device_id=device.id,
        previous_config=await mask_device_config(db, device, previous),
        new_config=await mask_device_config(db, device, merged),
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
    return await mask_device_config(db, device, device.device_config)


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
