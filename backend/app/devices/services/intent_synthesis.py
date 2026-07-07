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

from sqlalchemy import select

from app.devices.models import DeviceIntent, DeviceReservation
from app.devices.services.intent_types import GRID_ROUTING, PRIORITY_RUN_ROUTING

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device


async def synthesize_fact_intents(
    db: AsyncSession,
    device: Device,
    node: AppiumNode,
    stored: list[DeviceIntent],
    now: datetime,
) -> list[DeviceIntent]:
    """Return in-memory intents derived from domain facts. Grows per family."""
    return await _reservation_intents(db, device, now)


async def _reservation_intents(db: AsyncSession, device: Device, now: datetime) -> list[DeviceIntent]:
    """Grid routing (and, from Task 4, cooldown denies) derived from the active reservation row."""
    entry = (
        await db.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device.id, DeviceReservation.released_at.is_(None))
            .order_by(DeviceReservation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if entry is None:
        return []
    if entry.excluded and entry.excluded_until is None:
        # Indefinite (health-failure) exclusion: the stored run: twin was revoked
        # by exclude_run_if_needed; reproduce its absence.
        return []
    return [
        DeviceIntent(
            device_id=device.id,
            source=f"run:{entry.run_id}",
            axis=GRID_ROUTING,
            run_id=entry.run_id,
            payload={"accepting_new_sessions": True, "priority": PRIORITY_RUN_ROUTING},
        )
    ]
