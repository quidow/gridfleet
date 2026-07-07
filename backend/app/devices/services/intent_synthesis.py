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
from app.devices.services.intent_types import (
    GRID_ROUTING,
    NODE_PROCESS,
    PRIORITY_CONNECTIVITY_LOST,
    PRIORITY_COOLDOWN,
    PRIORITY_MAINTENANCE,
    PRIORITY_RUN_ROUTING,
    RECOVERY,
)
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON, in_maintenance

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device


async def synthesize_fact_intents(
    db: AsyncSession,
    device: Device,
    node: AppiumNode | None,
    stored: list[DeviceIntent],
    now: datetime,
) -> list[DeviceIntent]:
    """Return in-memory intents derived from domain facts. Grows per family.

    ``node`` is currently unused by every family; it is kept because callers already
    hold it and a future node-derived family may need it.
    """
    intents = await _reservation_intents(db, device, now)
    intents += _maintenance_intents(device)
    intents += _connectivity_intents(device, stored, now)
    return intents


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
        # Indefinite (health-failure) exclusion: no run routing, no cooldown denies.
        # The exclusion row is written directly by exclude_device_from_run.
        return []
    intents = [
        DeviceIntent(
            device_id=device.id,
            source=f"run:{entry.run_id}",
            axis=GRID_ROUTING,
            run_id=entry.run_id,
            payload={"accepting_new_sessions": True, "priority": PRIORITY_RUN_ROUTING},
        )
    ]
    if entry.excluded and entry.excluded_until is not None and entry.excluded_until > now:
        # Timed exclusion (cooldown): deny new sessions + recovery while it lasts, but
        # keep run routing (cooldown outranks it, it never revoked it). The cooldown row
        # is the state — cooldown:reservation gets no synthesized twin.
        intents.append(
            DeviceIntent(
                device_id=device.id,
                source=f"cooldown:grid:{entry.run_id}",
                axis=GRID_ROUTING,
                run_id=entry.run_id,
                payload={"accepting_new_sessions": False, "priority": PRIORITY_COOLDOWN},
            )
        )
        intents.append(
            DeviceIntent(
                device_id=device.id,
                source=f"cooldown:recovery:{entry.run_id}",
                axis=RECOVERY,
                run_id=entry.run_id,
                payload={"allowed": False, "priority": PRIORITY_COOLDOWN, "reason": entry.exclusion_reason},
            )
        )
    return intents


def _maintenance_intents(device: Device) -> list[DeviceIntent]:
    """Graceful stop + recovery deny derived from the maintenance_reason fact."""
    if not in_maintenance(device):
        return []
    return [
        DeviceIntent(
            device_id=device.id,
            source=f"maintenance:node:{device.id}",
            axis=NODE_PROCESS,
            payload={"action": "stop", "priority": PRIORITY_MAINTENANCE, "stop_mode": "graceful"},
        ),
        DeviceIntent(
            device_id=device.id,
            source=f"maintenance:recovery:{device.id}",
            axis=RECOVERY,
            # Reason must equal MAINTENANCE_HOLD_SUPPRESSION_REASON exactly:
            # clear_maintenance_recovery_suppression (exit_maintenance) only clears the
            # suppression when the stored value matches this constant. Any drift freezes
            # the node effective_state at "blocked" after an operator exit.
            payload={"allowed": False, "priority": PRIORITY_MAINTENANCE, "reason": MAINTENANCE_HOLD_SUPPRESSION_REASON},
        ),
    ]


def _connectivity_intents(device: Device, stored: list[DeviceIntent], now: datetime) -> list[DeviceIntent]:
    """Defer-stop derived from device_checks_healthy IS FALSE (the orphan sweep's own condition)."""
    if device.device_checks_healthy is not False:
        return []
    # An active stored start command (operator start/restart, verification lease,
    # auto-recovery) overrides the connectivity park — this rule replaces the old
    # scattered "revoke connectivity:* before starting" ritual (semantic delta #3).
    has_start_command = any(
        i.axis == NODE_PROCESS and i.payload.get("action") == "start" and (i.expires_at is None or i.expires_at > now)
        for i in stored
    )
    if has_start_command:
        return []
    return [
        DeviceIntent(
            device_id=device.id,
            source=f"connectivity:{device.id}",
            axis=NODE_PROCESS,
            payload={"action": "stop", "priority": PRIORITY_CONNECTIVITY_LOST, "stop_mode": "defer"},
        )
    ]
