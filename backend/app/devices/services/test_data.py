from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.devices.models import Device, DeviceTestDataAuditLog

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.protocols import EventPublisher


class TestDataService:
    __test__ = False  # not a pytest test class; manages device test-data payloads

    def __init__(self, *, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def get_device_test_data(self, db: AsyncSession, device: Device) -> dict[str, Any]:
        return copy.deepcopy(device.test_data or {})

    async def replace_device_test_data(
        self, db: AsyncSession, device: Device, data: dict[str, Any], *, changed_by: str | None = None
    ) -> dict[str, Any]:
        previous = device.test_data or {}
        device.test_data = data
        db.add(
            DeviceTestDataAuditLog(
                device_id=device.id,
                previous_test_data=copy.deepcopy(previous),
                new_test_data=copy.deepcopy(data),
                changed_by=changed_by,
            )
        )
        self._publisher.queue_for_session(
            db,
            "test_data.updated",
            {"device_id": str(device.id), "device_name": device.name, "changed_by": changed_by},
        )
        await db.commit()
        await db.refresh(device)
        return copy.deepcopy(device.test_data or {})

    async def merge_device_test_data(
        self, db: AsyncSession, device: Device, data: dict[str, Any], *, changed_by: str | None = None
    ) -> dict[str, Any]:
        previous = device.test_data or {}
        merged = _deep_merge(previous, data)
        return await self.replace_device_test_data(db, device, merged, changed_by=changed_by)

    async def get_test_data_history(
        self, db: AsyncSession, device_id: uuid.UUID, *, limit: int = 50
    ) -> list[DeviceTestDataAuditLog]:
        stmt = (
            select(DeviceTestDataAuditLog)
            .where(DeviceTestDataAuditLog.device_id == device_id)
            .order_by(DeviceTestDataAuditLog.changed_at.desc())
            .limit(limit)
        )
        return list((await db.execute(stmt)).scalars().all())


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result
