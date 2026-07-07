"""Fact-derived intent synthesis.

Derivable intents are not stored in ``device_intents``; they are synthesized
in-memory at evaluation time from the domain rows that used to back their
revoke paths and preconditions. Stored rows are reserved for commands and
leases that cannot be recomputed from facts (operator start/stop, verification,
auto-recovery, forced release, device delete, health-failure park).

Synthesized ``DeviceIntent`` objects are transient ORM instances — they are
never ``db.add()``-ed (same pattern as the ``baseline:idle`` synthesis that
predates this module).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device, DeviceIntent


async def synthesize_fact_intents(
    db: AsyncSession,
    device: Device,
    node: AppiumNode,
    stored: list[DeviceIntent],
    now: datetime,
) -> list[DeviceIntent]:
    """Return in-memory intents derived from domain facts. Grows per family."""
    del db, device, node, stored, now  # populated family-by-family in follow-up commits
    return []
