"""Row-level locking helper for the AppiumNode table.

INVARIANT: Any code path that writes ``AppiumNode.pid``, ``AppiumNode.port``,
``AppiumNode.active_connection_target``, or node health-observation fields must
acquire the row lock via ``lock_appium_node_for_device`` within the same
transaction as the write.

LOCK ORDERING (deadlock avoidance): When a code path also locks the device row,
it MUST acquire the locks in the order **Device first, then AppiumNode**. All
existing single-row callers in the codebase that lock a device do so via
``device_locking.lock_device``; AppiumNode locks are always taken AFTER the
device row lock has been acquired.

POPULATE_EXISTING: ``lock_appium_node_for_device`` uses
``execution_options(populate_existing=True)`` so the ORM identity map is
refreshed from the locked row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.appium_nodes.models import AppiumNode

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


async def lock_appium_node_for_device(
    db: AsyncSession,
    device_id: uuid.UUID,
) -> AppiumNode | None:
    """Acquire ``SELECT … FOR UPDATE`` on the AppiumNode row for ``device_id``.

    Returns ``None`` when no row exists for ``device_id``. Callers must hold
    the device row lock (via ``device_locking.lock_device``) before calling
    this helper to satisfy the documented Device→AppiumNode lock ordering.
    """
    stmt = (
        select(AppiumNode)
        .where(AppiumNode.device_id == device_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
