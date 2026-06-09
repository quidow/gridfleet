"""Column-backed device health service."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import locking as appium_node_locking
from app.core.sentinels import UNSET, UnsetType
from app.devices import locking as device_locking
from app.devices.services.health_view import (
    build_public_summary,
    device_allows_allocation,
    merged_liveness,
    node_running_signal,
)
from app.devices.services.intent import IntentService

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.devices.models import Device
    from app.events.protocols import EventPublisher

__all__ = [
    "DeviceHealthService",
    "build_public_summary",
    "device_allows_allocation",
    "merged_liveness",
]


def _node_reason_label(node: AppiumNode) -> str:
    if node.health_state:
        return node.health_state
    return "running" if node_running_signal(node) else "stopped"


def _now() -> datetime:
    return datetime.now(UTC)


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


def _verdict_changed(previous: dict[str, Any], device: Device) -> bool:
    """True if the device's public health verdict differs from ``previous``.

    Reuses the same per-verdict snapshot that drives ``_maybe_emit_health_changed``
    so the dirty-mark gate and the event-emission gate stay in lockstep.
    """
    return _status_snapshot(previous) != _status_snapshot(build_public_summary(device))


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
        locked.device_checks_checked_at = _now()
        # Reconcile immediately only on failure so the device goes offline right
        # away. On success, defer to apply_node_state_transition (which reconciles
        # on state transitions) or the background reconciler. This preserves the old
        # behavior: a healthy device_checks signal alone does not restore an
        # offline device — the node must also be observed running.
        if not healthy:
            await IntentService(db).mark_dirty_and_reconcile(locked.id, reason=summary, publisher=self._publisher)
        elif _verdict_changed(previous, locked):
            # Steady-state healthy re-marks are the dominant reconciler churn — only
            # enqueue when the public verdict actually transitions. The full scan is
            # the backstop for any drift that has no observation transition.
            await IntentService(db).mark_dirty(locked.id, reason="device checks healthy")
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
        locked.session_viability_checked_at = _now()
        # Same asymmetry as update_device_checks: reconcile immediately on failure
        # (device goes offline), defer on success (rely on apply_node_state_transition).
        if status == "failed":
            await IntentService(db).mark_dirty_and_reconcile(
                locked.id, reason=error or "session viability failed", publisher=self._publisher
            )
        elif _verdict_changed(previous, locked):
            await IntentService(db).mark_dirty(locked.id, reason="session viability passed")

        _maybe_emit_health_changed(db, locked, previous, publisher=self._publisher)

    async def apply_node_state_transition(
        self,
        db: AsyncSession,
        device: Device,
        *,
        health_running: bool | None | UnsetType = UNSET,
        health_state: str | None | UnsetType = UNSET,
        mark_offline: bool = True,
        reason: str | None = None,
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
        prev_running = locked_node.health_running
        prev_state = locked_node.health_state
        health_provided = not isinstance(health_running, UnsetType) or not isinstance(health_state, UnsetType)
        if not isinstance(health_running, UnsetType):
            locked_node.health_running = health_running
        if not isinstance(health_state, UnsetType):
            locked_node.health_state = health_state
        if health_provided:
            locked_node.last_health_checked_at = _now()

        # Transition gate: an explicit health observation that does not change the
        # node's health columns is the steady-state node-health churn (node_health
        # re-asserts health_running=True/health_state=None every cycle). Skip the
        # reconcile/mark in that case. Callers that make NO health statement (UNSET,
        # e.g. mark_node_started after setting pid) and explicit mark_offline=True
        # must still act — operational_state (not part of the public verdict) may
        # need re-derivation, and offline is an explicit intent. The full scan is the
        # backstop for any drift with no observation transition.
        # health_running is not True covers the recovery/None path: health_running=None
        # (clear) or =False are not the confirmed-running steady state, so they must
        # always act even when the column value is already the same (e.g. a fresh node
        # with health_running=None being probed again during maintenance recovery).
        running_changed = not isinstance(health_running, UnsetType) and health_running != prev_running
        state_changed = not isinstance(health_state, UnsetType) and health_state != prev_state
        health_changed = running_changed or state_changed
        should_act = mark_offline or not health_provided or health_changed or health_running is not True

        # Reconcile when: (a) mark_offline=True (explicit offline intent), or
        # (b) the call clears or does not touch the health signal (→ may restore
        # to available). Do NOT reconcile when mark_offline=False and
        # health_running=False (below-threshold failure recording — hysteresis:
        # let the threshold be reached before offline derivation).
        should_reconcile = mark_offline or health_running is not False
        if should_act and should_reconcile:
            await IntentService(db).mark_dirty_and_reconcile(
                locked.id,
                reason=reason or f"node: {_node_reason_label(locked_node)}",
                publisher=self._publisher,
            )
        elif should_act:
            await IntentService(db).mark_dirty(
                locked.id,
                reason=reason or f"node: {_node_reason_label(locked_node)}",
            )
        _maybe_emit_health_changed(db, locked, previous, publisher=self._publisher)

    async def update_emulator_state(self, db: AsyncSession, device: Device, state: str | None) -> None:
        locked = await _lock(db, device)
        if locked is None:
            return
        locked.emulator_state = state
