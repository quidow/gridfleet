"""Row-level locking helper for the Device table.

INVARIANT: Any code path that writes ``Device.operational_state``,
``Device.hold``, or ``Device.lifecycle_policy_state`` must acquire the row lock
via ``lock_device`` within the same transaction as the write.

DEADLOCK AVOIDANCE: Multi-row callers must use ``lock_devices``, which orders
ids ascending. Mixing single-row and batch callers stays deadlock-free as long
as the batch order matches.

EAGER LOADS: ``lock_device`` always eager-loads ``appium_node`` and ``host``.
Pass ``load_sessions=True`` to additionally eager-load ``Device.sessions`` —
required by lifecycle_policy callers that read session-related state inside
the locked transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.device import Device

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def lock_device(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    load_sessions: bool = False,
) -> Device:
    options: list[Any] = [selectinload(Device.appium_node), selectinload(Device.host)]
    if load_sessions:
        options.append(selectinload(Device.sessions))
    stmt = (
        select(Device)
        .where(Device.id == device_id)
        .options(*options)
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
