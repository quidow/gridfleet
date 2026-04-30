"""Row-level locking helper for the Device table.

INVARIANT: Any code path that writes ``Device.availability_status`` or
``Device.lifecycle_policy_state`` must acquire the row lock via ``lock_device``
within the same transaction as the write.

DEADLOCK AVOIDANCE: Multi-row callers must use ``lock_devices``, which orders
ids ascending. Mixing single-row and batch callers stays deadlock-free as long
as the batch order matches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.device import Device

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def lock_device(db: AsyncSession, device_id: uuid.UUID) -> Device:
    stmt = (
        select(Device)
        .where(Device.id == device_id)
        .options(selectinload(Device.appium_node), selectinload(Device.host))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one()


async def lock_devices(db: AsyncSession, device_ids: list[uuid.UUID]) -> list[Device]:
    if not device_ids:
        return []
    ordered = sorted(set(device_ids))
    stmt = (
        select(Device)
        .where(Device.id.in_(ordered))
        .options(selectinload(Device.appium_node), selectinload(Device.host))
        .order_by(Device.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return list((await db.execute(stmt)).scalars().all())
