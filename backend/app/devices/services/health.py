"""Column-backed device health service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import locking as appium_node_locking
from app.core.sentinels import UNSET, UnsetType
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.services.health_view import (
    build_public_summary,
    device_allows_allocation,
    merged_liveness,
)
from app.devices.services.intent import IntentService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device
    from app.events.protocols import EventPublisher

__all__ = [
    "DeviceHealthService",
    "build_public_summary",
    "device_allows_allocation",
    "merged_liveness",
]


async def _lock(db: AsyncSession, device: Device) -> Device | None:
    try:
        return await device_locking.lock_device(db, device.id)
    except NoResultFound:
        return None


def _status_snapshot(summary: dict[str, Any]) -> tuple[Any, ...]:
    return (
        summary["overall"],
        summary["device"]["status"],
        summary["node"]["status"],
        summary["viability"]["status"],
    )


def _maybe_emit_health_changed(
    db: AsyncSession,
    device: Device,
    previous: dict[str, Any],
    *,
    publisher: EventPublisher,
) -> None:
    nxt = build_public_summary(device)
    if _status_snapshot(previous) == _status_snapshot(nxt):
        return
    publisher.queue_for_session(
        db,
        "device.health_changed",
        {
            "device_id": str(device.id),
            "overall": nxt["overall"],
            "device": nxt["device"],
            "node": nxt["node"],
            "viability": nxt["viability"],
        },
    )


class DeviceHealthService:
    def __init__(self, *, publisher: EventPublisher) -> None:
        self._publisher = publisher

    async def update_device_checks(self, db: AsyncSession, device: Device, *, healthy: bool, summary: str) -> None:
        locked = await _lock(db, device)
        if locked is None:
            return
        previous = build_public_summary(locked)
        locked.device_checks_healthy = healthy
        locked.device_checks_summary = summary
        locked.device_checks_checked_at = now_utc()
        if not healthy:
            await IntentService(db).reconcile_now(locked.id, publisher=self._publisher)
        # On success, defer to apply_node_state_transition (which reconciles on
        # state transitions) or the next reconciler scan tick (≤ one
        # intent_reconcile_interval): a healthy device_checks signal alone does
        # not restore an offline device — the node must also be observed running.
        _maybe_emit_health_changed(db, locked, previous, publisher=self._publisher)

    async def update_session_viability(
        self, db: AsyncSession, device: Device, *, status: str | None, error: str | None
    ) -> None:
        locked = await _lock(db, device)
        if locked is None:
            return
        previous = build_public_summary(locked)
        locked.session_viability_status = status
        locked.session_viability_error = error
        locked.session_viability_checked_at = now_utc()
        # Same asymmetry as update_device_checks: reconcile immediately on failure
        # (device goes offline), defer on success (rely on apply_node_state_transition
        # or the next reconciler scan tick).
        _maybe_emit_health_changed(db, locked, previous, publisher=self._publisher)

    async def apply_node_state_transition(
        self,
        db: AsyncSession,
        device: Device,
        *,
        health_running: bool | None | UnsetType = UNSET,
        health_state: str | None | UnsetType = UNSET,
        mark_offline: bool = True,
    ) -> None:
        locked = await _lock(db, device)
        if locked is None:
            return
        locked_node = await appium_node_locking.lock_appium_node_for_device(db, locked.id)
        if locked_node is None:
            return

        locked.appium_node = locked_node
        previous = build_public_summary(locked)

        # UNSET = caller is not making a health statement: leave the columns
        # (and the checked-at stamp) untouched. Explicit None = clear.
        health_provided = not isinstance(health_running, UnsetType) or not isinstance(health_state, UnsetType)
        if not isinstance(health_running, UnsetType):
            locked_node.health_running = health_running
        if not isinstance(health_state, UnsetType):
            locked_node.health_state = health_state
        if health_provided:
            locked_node.last_health_checked_at = now_utc()

        # Agent-visible reactions remain on the connectivity/lifecycle intent
        # paths; operational state is projected at read time.
        _maybe_emit_health_changed(db, locked, previous, publisher=self._publisher)

    async def update_emulator_state(self, db: AsyncSession, device: Device, state: str | None) -> None:
        locked = await _lock(db, device)
        if locked is None:
            return
        locked.emulator_state = state
