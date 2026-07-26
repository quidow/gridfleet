from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.devices import locking as device_locking
from app.devices.models import Device, DeviceTestDataAuditLog

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.locking import LockedDevice
    from app.events.protocols import EventPublisher


class TestDataService:
    __test__ = False  # not a pytest test class; manages device test-data payloads

    def __init__(self, *, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def get_device_test_data(self, db: AsyncSession, device: Device) -> dict[str, Any]:
        return copy.deepcopy(device.test_data or {})

    async def replace_device_test_data(
        self, db: AsyncSession, device_id: uuid.UUID, data: dict[str, Any], *, changed_by: str | None = None
    ) -> dict[str, Any]:
        """Overwrite the device's test data inside the caller's transaction.

        Raises ``NoResultFound`` when the device does not exist, which the router
        maps to the same 404 body its old pre-lock produced.
        """
        locked = await device_locking.lock_device_handle(db, device_id)
        return await self._write_test_data(db, locked, data, changed_by=changed_by)

    async def merge_device_test_data(
        self, db: AsyncSession, device_id: uuid.UUID, data: dict[str, Any], *, changed_by: str | None = None
    ) -> dict[str, Any]:
        locked = await device_locking.lock_device_handle(db, device_id)
        merged = _deep_merge(locked.device.test_data or {}, data)
        return await self._write_test_data(db, locked, merged, changed_by=changed_by)

    async def _write_test_data(
        self, db: AsyncSession, locked: LockedDevice, data: dict[str, Any], *, changed_by: str | None
    ) -> dict[str, Any]:
        """Stage the mutation, its audit row, and its event under one Device proof."""
        locked.assert_active(db)
        device = locked.device
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
        await db.flush()
        return copy.deepcopy(device.test_data or {})

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
