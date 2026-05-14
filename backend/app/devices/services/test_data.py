import copy
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices.models import Device, DeviceTestDataAuditLog
from app.events import queue_event_for_session


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


async def get_device_test_data(db: AsyncSession, device: Device) -> dict[str, Any]:
    return copy.deepcopy(device.test_data or {})


async def replace_device_test_data(
    db: AsyncSession,
    device: Device,
    new_data: dict[str, Any],
    *,
    changed_by: str | None = None,
) -> dict[str, Any]:
    previous = device.test_data or {}
    device.test_data = new_data
    db.add(
        DeviceTestDataAuditLog(
            device_id=device.id,
            previous_test_data=copy.deepcopy(previous),
            new_test_data=copy.deepcopy(new_data),
            changed_by=changed_by,
        )
    )
    queue_event_for_session(
        db,
        "test_data.updated",
        {"device_id": str(device.id), "device_name": device.name, "changed_by": changed_by},
    )
    await db.commit()
    await db.refresh(device)
    return copy.deepcopy(device.test_data or {})


async def merge_device_test_data(
    db: AsyncSession,
    device: Device,
    partial: dict[str, Any],
    *,
    changed_by: str | None = None,
) -> dict[str, Any]:
    previous = device.test_data or {}
    merged = _deep_merge(previous, partial)
    return await replace_device_test_data(db, device, merged, changed_by=changed_by)


async def get_test_data_history(
    db: AsyncSession, device_id: uuid.UUID, *, limit: int = 50
) -> list[DeviceTestDataAuditLog]:
    stmt = (
        select(DeviceTestDataAuditLog)
        .where(DeviceTestDataAuditLog.device_id == device_id)
        .order_by(DeviceTestDataAuditLog.changed_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())
