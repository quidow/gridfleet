"""Column-backed device health service."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import NoResultFound

from app.appium_nodes.services import locking as appium_node_locking
from app.core.observation_revision import next_observation_revision
from app.core.sentinels import UNSET, UnsetType
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.health_view import (
    build_public_summary,
    device_allows_allocation,
    merged_liveness,
)
from app.devices.services.intent import IntentService
from app.devices.services.lifecycle_policy_state import clear_recovery_generation
from app.lifecycle.services import remediation_log

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.models import AppiumNode
    from app.devices.locking import LockedDevice
    from app.devices.models import Device
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot
    from app.devices.services.device_health_fold_context import LockedDeviceFold
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


async def _lock_handle(db: AsyncSession, device: Device) -> LockedDevice | None:
    try:
        return await device_locking.lock_device_handle(db, device.id)
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
    policy_view: dict[str, Any],
    publisher: EventPublisher,
) -> None:
    nxt = build_public_summary(device, policy_view=policy_view)
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

    async def update_device_checks(
        self,
        db: AsyncSession,
        device: Device,
        *,
        healthy: bool,
        summary: str,
        revision: int | None = None,
        observed_at: datetime | None = None,
    ) -> bool:
        locked = await _lock_handle(db, device)
        if locked is None:
            return False
        snapshot = await load_device_decision_snapshot(db, locked, packs={}, now=now_utc())
        updated = await self.update_device_checks_locked(
            db,
            locked,
            snapshot,
            healthy=healthy,
            summary=summary,
            revision=revision,
            observed_at=observed_at,
        )
        return updated is not None

    async def update_locked_device_checks(
        self,
        db: AsyncSession,
        locked: LockedDeviceFold,
        snapshot: DeviceDecisionSnapshot,
        *,
        healthy: bool,
        summary: str,
        revision: int | None = None,
        observed_at: datetime | None = None,
    ) -> DeviceDecisionSnapshot | None:
        return await self.update_device_checks_locked(
            db,
            locked.locked_device,
            snapshot,
            healthy=healthy,
            summary=summary,
            revision=revision,
            observed_at=observed_at,
        )

    async def update_device_checks_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        snapshot: DeviceDecisionSnapshot,
        *,
        healthy: bool,
        summary: str,
        revision: int | None = None,
        observed_at: datetime | None = None,
    ) -> DeviceDecisionSnapshot | None:
        locked.assert_active(db)
        result = await self._update_locked_device_checks_row(
            db,
            locked.device,
            snapshot,
            healthy=healthy,
            summary=summary,
            revision=revision,
            observed_at=observed_at,
        )
        if result is None:
            return None
        previous, policy_view, updated = result
        if not healthy:
            if snapshot.recovery_generation is not None:
                clear_recovery_generation(locked.device, expected=snapshot.recovery_generation)
            await IntentService(db).reconcile_locked(locked, publisher=self._publisher, snapshot=updated)
        _maybe_emit_health_changed(db, locked.device, previous, policy_view=policy_view, publisher=self._publisher)
        return updated

    async def _update_locked_device_checks_row(
        self,
        db: AsyncSession,
        locked: Device,
        snapshot: DeviceDecisionSnapshot,
        *,
        healthy: bool,
        summary: str,
        revision: int | None,
        observed_at: datetime | None,
    ) -> tuple[dict[str, Any], dict[str, Any], DeviceDecisionSnapshot] | None:
        # Two-axis guard: a synchronous higher-authority writer passes no revision
        # and draws a fresh one at write time, so it always out-ranks a stale fold
        # observation whose (lower) revision was drawn earlier at ingest. A moved
        # fold passes its ingest-time revision and loses the strictly-greater
        # comparison when a fresher write landed first.
        rev = revision if revision is not None else await next_observation_revision(db)
        if rev <= locked.device_checks_observation_revision:
            return None
        policy_view = remediation_log.build_policy_view(snapshot.ladder, locked.lifecycle_policy_state)
        previous = build_public_summary(locked, policy_view=policy_view)
        was_failing = locked.device_checks_healthy is False
        locked.device_checks_healthy = healthy
        locked.device_checks_summary = summary
        locked.device_checks_checked_at = observed_at or now_utc()
        locked.device_checks_observation_revision = rev
        if healthy:
            locked.failure_episode_id = None
        elif not was_failing:
            locked.failure_episode_id = uuid.uuid4()
        # On success, defer to apply_node_state_transition (which reconciles on
        # state transitions) or the next reconciler scan tick (≤ one
        # intent_reconcile_interval): a healthy device_checks signal alone does
        # not restore an offline device — the node must also be observed running.
        updated = replace(
            snapshot,
            decision_facts=replace(snapshot.decision_facts, device_checks_unhealthy=not healthy),
            state_facts=replace(
                snapshot.state_facts,
                ready=(
                    snapshot.is_ready_for_use
                    and device_allows_allocation(locked)
                    and not snapshot.state_facts.in_maintenance
                    and not snapshot.review_required
                ),
            ),
            recovery_generation=None if not healthy else snapshot.recovery_generation,
        )
        return previous, policy_view, updated

    async def update_session_viability(
        self, db: AsyncSession, device: Device, *, status: str | None, error: str | None
    ) -> None:
        locked = await _lock(db, device)
        if locked is None:
            return
        ladder = await remediation_log.load_ladder(db, locked.id)
        policy_view = remediation_log.build_policy_view(ladder, locked.lifecycle_policy_state)
        previous = build_public_summary(locked, policy_view=policy_view)
        locked.session_viability_status = status
        locked.session_viability_error = error
        locked.session_viability_checked_at = now_utc()
        # Same asymmetry as update_device_checks: reconcile immediately on failure
        # (device goes offline), defer on success (rely on apply_node_state_transition
        # or the next reconciler scan tick).
        _maybe_emit_health_changed(db, locked, previous, policy_view=policy_view, publisher=self._publisher)

    async def apply_node_state_transition(
        self,
        db: AsyncSession,
        device: Device,
        *,
        health_running: bool | None | UnsetType = UNSET,
        health_state: str | None | UnsetType = UNSET,
        mark_offline: bool = True,
        revision: int | None = None,
        observed_at: datetime | None = None,
    ) -> None:
        locked = await _lock_handle(db, device)
        if locked is None:
            return

        snapshot = await load_device_decision_snapshot(db, locked, packs={}, now=now_utc())

        locked_node = await appium_node_locking.lock_appium_node_for_device(db, locked.device.id)
        if locked_node is None:
            return

        await self.apply_locked_node_state_transition(
            db,
            locked,
            locked_node,
            snapshot,
            health_running=health_running,
            health_state=health_state,
            mark_offline=mark_offline,
            revision=revision,
            observed_at=observed_at,
        )

    async def apply_locked_node_state_transition(  # noqa: PLR0913
        self,
        db: AsyncSession,
        locked: LockedDevice,
        locked_node: AppiumNode,
        snapshot: DeviceDecisionSnapshot,
        *,
        health_running: bool | None | UnsetType = UNSET,
        health_state: str | None | UnsetType = UNSET,
        mark_offline: bool = True,
        revision: int | None = None,
        observed_at: datetime | None = None,
    ) -> DeviceDecisionSnapshot:
        locked.assert_active(db)
        device = locked.device
        device.appium_node = locked_node
        previous = build_public_summary(
            device,
            policy_view=remediation_log.build_policy_view(snapshot.ladder, device.lifecycle_policy_state),
        )
        prev_running = locked_node.health_running
        prev_state = locked_node.health_state
        health_provided = not isinstance(health_running, UnsetType) or not isinstance(health_state, UnsetType)
        if health_provided:
            rev = revision if revision is not None else await next_observation_revision(db)
            if rev <= locked_node.health_observation_revision:
                return snapshot
            locked_node.last_health_checked_at = observed_at or now_utc()
            locked_node.health_observation_revision = rev
        if not isinstance(health_running, UnsetType):
            locked_node.health_running = health_running
        if not isinstance(health_state, UnsetType):
            locked_node.health_state = health_state
        running_changed = not isinstance(health_running, UnsetType) and health_running != prev_running
        state_changed = not isinstance(health_state, UnsetType) and health_state != prev_state
        health_changed = running_changed or state_changed
        should_act = mark_offline or not health_provided or health_changed or health_running is not True
        should_reconcile = mark_offline or health_running is not False
        updated_state = replace(
            snapshot.state_facts,
            ready=(
                snapshot.is_ready_for_use
                and device_allows_allocation(device)
                and not snapshot.state_facts.in_maintenance
                and not snapshot.review_required
            ),
        )
        updated = replace(
            snapshot,
            state_facts=updated_state,
            node_observed_running=locked_node.observed_running,
        )
        if should_act and should_reconcile:
            await IntentService(db).reconcile_locked(locked, publisher=self._publisher, snapshot=updated)
        _maybe_emit_health_changed(
            db,
            device,
            previous,
            policy_view=remediation_log.build_policy_view(updated.ladder, device.lifecycle_policy_state),
            publisher=self._publisher,
        )
        return updated
