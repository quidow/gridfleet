from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select

from app import metrics_recorders
from app.database import async_session
from app.models.agent_reconfigure_outbox import AgentReconfigureOutbox
from app.models.appium_node import AppiumDesiredState
from app.models.device_event import DeviceEventType
from app.models.device_intent import DeviceIntent
from app.models.device_intent_dirty import DeviceIntentDirty
from app.models.device_reservation import DeviceReservation
from app.services import device_locking
from app.services.agent_reconfigure_delivery import deliver_agent_reconfigures
from app.services.desired_state_writer import write_desired_grid_run_id, write_desired_state
from app.services.device_event_service import record_event
from app.services.intent_evaluator import (
    ReservationDecision,
    evaluate_grid_routing,
    evaluate_node_process,
    evaluate_recovery,
    evaluate_reservation,
    map_node_process_decision,
)
from app.services.intent_types import GRID_ROUTING, NODE_PROCESS, PRIORITY_IDLE, RECOVERY, RESERVATION
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.appium_node import AppiumNode


async def device_intent_reconciler_loop() -> None:
    cycle = 0
    while True:
        interval = int(settings_service.get("general.intent_reconcile_interval_sec"))
        full_scan_every = int(settings_service.get("general.intent_reconcile_full_scan_every_cycles"))
        async with async_session() as db:
            await _reconcile_expired_intents(db)
            if cycle % full_scan_every == 0:
                await _reconcile_all_devices_once(db)
            else:
                await _reconcile_dirty_devices(db)
        cycle += 1
        await asyncio.sleep(interval)


async def _reconcile_all_devices_once(db: AsyncSession) -> None:
    rows = (await db.execute(select(DeviceIntent.device_id).distinct())).scalars().all()
    for device_id in rows:
        await _reconcile_device(db, device_id)
        await db.commit()
        await deliver_agent_reconfigures(db, device_id)


async def _reconcile_dirty_devices(db: AsyncSession, *, limit: int = 100) -> None:
    queue_size = await db.scalar(select(func.count()).select_from(DeviceIntentDirty))
    metrics_recorders.INTENT_RECONCILER_DIRTY_QUEUE_SIZE.set(int(queue_size or 0))
    rows = (
        (await db.execute(select(DeviceIntentDirty).order_by(DeviceIntentDirty.dirty_at).limit(limit))).scalars().all()
    )
    for row in rows:
        device_id = row.device_id
        generation = row.generation
        await _reconcile_device(db, device_id)
        current = await db.get(DeviceIntentDirty, device_id, populate_existing=True)
        if current is not None and current.generation == generation:
            await db.delete(current)
        await db.commit()
        await deliver_agent_reconfigures(db, device_id)


async def _reconcile_expired_intents(db: AsyncSession) -> None:
    now = datetime.now(UTC)
    device_ids = (
        (
            await db.execute(
                select(DeviceIntent.device_id).where(
                    DeviceIntent.expires_at.is_not(None), DeviceIntent.expires_at <= now
                )
            )
        )
        .scalars()
        .all()
    )
    if not device_ids:
        return
    await db.execute(delete(DeviceIntent).where(DeviceIntent.expires_at.is_not(None), DeviceIntent.expires_at <= now))
    for device_id in sorted(set(device_ids)):
        await _reconcile_device(db, device_id)
    await db.flush()


async def _reconcile_device(db: AsyncSession, device_id: uuid.UUID) -> None:
    metrics_recorders.INTENT_RECONCILER_EVALUATIONS.inc()
    device = await device_locking.lock_device(db, device_id)
    node = device.appium_node
    if node is None:
        return

    now = datetime.now(UTC)
    intents = (
        (
            await db.execute(
                select(DeviceIntent).where(DeviceIntent.device_id == device_id).order_by(DeviceIntent.source)
            )
        )
        .scalars()
        .all()
    )
    intent_count = await db.scalar(select(func.count()).select_from(DeviceIntent))
    metrics_recorders.INTENT_REGISTRY_INTENTS.set(int(intent_count or 0))
    active_node_intents = [
        intent
        for intent in intents
        if intent.axis == NODE_PROCESS and (intent.expires_at is None or intent.expires_at > now)
    ]
    if not active_node_intents and device.auto_manage and device.verified_at is not None:
        intents = [
            *intents,
            DeviceIntent(
                device_id=device_id,
                source="baseline:idle",
                axis=NODE_PROCESS,
                payload={"action": "start", "priority": PRIORITY_IDLE, "desired_port": node.port},
            ),
        ]

    node_decision = evaluate_node_process([intent for intent in intents if intent.axis == NODE_PROCESS], now)
    grid_decision = evaluate_grid_routing([intent for intent in intents if intent.axis == GRID_ROUTING], now)
    reservation_decision = evaluate_reservation([intent for intent in intents if intent.axis == RESERVATION], now)
    recovery_decision = evaluate_recovery([intent for intent in intents if intent.axis == RECOVERY], now)
    target_state, node_accepting_new_sessions, stop_pending = map_node_process_decision(node_decision)
    accepting_new_sessions = node_accepting_new_sessions and grid_decision.accepting_new_sessions

    old = {
        "desired_state": node.desired_state,
        "desired_port": node.desired_port,
        "desired_grid_run_id": node.desired_grid_run_id,
        "accepting_new_sessions": node.accepting_new_sessions,
        "stop_pending": node.stop_pending,
        "recovery_allowed": device.recovery_allowed,
        "recovery_blocked_reason": device.recovery_blocked_reason,
    }

    desired_port = node_decision.desired_port if target_state == AppiumDesiredState.running else None
    if target_state == AppiumDesiredState.running and desired_port is None:
        desired_port = node.port
    await write_desired_state(
        db,
        node=node,
        target=target_state,
        desired_port=desired_port,
        transition_token=node_decision.transition_token,
        transition_deadline=node_decision.transition_deadline,
        caller="intent_reconciler",
        reason=node_decision.reason,
    )
    await write_desired_grid_run_id(
        db,
        node=node,
        run_id=grid_decision.run_id,
        caller="intent_reconciler",
        reason=grid_decision.reason,
    )

    if node.accepting_new_sessions != accepting_new_sessions:
        await _record_field_change(
            db,
            device_id,
            "accepting_new_sessions",
            node.accepting_new_sessions,
            accepting_new_sessions,
            grid_decision.reason,
        )
        node.accepting_new_sessions = accepting_new_sessions
    if node.stop_pending != stop_pending:
        await _record_field_change(db, device_id, "stop_pending", node.stop_pending, stop_pending, node_decision.reason)
        node.stop_pending = stop_pending

    if device.recovery_allowed != recovery_decision.allowed:
        await _record_field_change(
            db,
            device_id,
            "recovery_allowed",
            device.recovery_allowed,
            recovery_decision.allowed,
            recovery_decision.reason,
        )
        device.recovery_allowed = recovery_decision.allowed
    if device.recovery_blocked_reason != recovery_decision.reason:
        await _record_field_change(
            db,
            device_id,
            "recovery_blocked_reason",
            device.recovery_blocked_reason,
            recovery_decision.reason,
            recovery_decision.reason,
        )
        device.recovery_blocked_reason = recovery_decision.reason

    await _apply_reservation_decision(db, device_id, reservation_decision)

    metadata_changed = (
        old["accepting_new_sessions"] != node.accepting_new_sessions
        or old["stop_pending"] != node.stop_pending
        or old["desired_grid_run_id"] != node.desired_grid_run_id
    )
    changed = metadata_changed or any(
        old[key] != getattr(node if key.startswith("desired") else device, key)
        for key in ("desired_state", "desired_port", "recovery_allowed", "recovery_blocked_reason")
    )
    if changed:
        node.generation += 1
    if metadata_changed and node.desired_state == AppiumDesiredState.running:
        _stage_agent_reconfigure(db, node)
    await db.flush()


async def _apply_reservation_decision(db: AsyncSession, device_id: uuid.UUID, decision: ReservationDecision) -> None:
    reservation = (
        await db.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device_id, DeviceReservation.released_at.is_(None))
            .order_by(DeviceReservation.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if reservation is None:
        return
    if decision.excluded:
        await _update_reservation_exclusion(db, reservation, decision)
    else:
        await _clear_reservation_exclusion(db, reservation, decision.reason)


async def _update_reservation_exclusion(
    db: AsyncSession,
    reservation: DeviceReservation,
    decision: ReservationDecision,
) -> None:
    changed = (
        reservation.excluded is not True
        or reservation.exclusion_reason != decision.exclusion_reason
        or reservation.excluded_until != decision.expires_at
        or reservation.cooldown_count != (decision.cooldown_count or 0)
    )
    if not changed:
        return
    old = {
        "excluded": reservation.excluded,
        "exclusion_reason": reservation.exclusion_reason,
        "excluded_until": reservation.excluded_until.isoformat() if reservation.excluded_until else None,
        "cooldown_count": reservation.cooldown_count,
    }
    reservation.excluded = True
    reservation.exclusion_reason = decision.exclusion_reason
    reservation.excluded_until = decision.expires_at
    reservation.cooldown_count = decision.cooldown_count or 0
    if reservation.excluded_at is None:
        reservation.excluded_at = datetime.now(UTC)
    await record_event(
        db,
        reservation.device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": "reservation_exclusion",
            "old_value": old,
            "new_value": {
                "excluded": reservation.excluded,
                "exclusion_reason": reservation.exclusion_reason,
                "excluded_until": reservation.excluded_until.isoformat() if reservation.excluded_until else None,
                "cooldown_count": reservation.cooldown_count,
            },
            "caller": "intent_reconciler",
            "reason": decision.reason,
        },
    )


async def _clear_reservation_exclusion(db: AsyncSession, reservation: DeviceReservation, reason: str) -> None:
    if not reservation.excluded and reservation.exclusion_reason is None and reservation.excluded_until is None:
        return
    old = {
        "excluded": reservation.excluded,
        "exclusion_reason": reservation.exclusion_reason,
        "excluded_until": reservation.excluded_until.isoformat() if reservation.excluded_until else None,
        "cooldown_count": reservation.cooldown_count,
    }
    reservation.excluded = False
    reservation.exclusion_reason = None
    reservation.excluded_until = None
    reservation.cooldown_count = 0
    await record_event(
        db,
        reservation.device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": "reservation_exclusion",
            "old_value": old,
            "new_value": {"excluded": False, "exclusion_reason": None, "excluded_until": None, "cooldown_count": 0},
            "caller": "intent_reconciler",
            "reason": reason,
        },
    )


def _stage_agent_reconfigure(db: AsyncSession, node: AppiumNode) -> None:
    db.add(
        AgentReconfigureOutbox(
            device_id=node.device_id,
            port=node.port,
            accepting_new_sessions=node.accepting_new_sessions,
            stop_pending=node.stop_pending,
            grid_run_id=node.desired_grid_run_id,
            reconciled_generation=node.generation,
        )
    )


async def _record_field_change(
    db: AsyncSession,
    device_id: uuid.UUID,
    field: str,
    old_value: object,
    new_value: object,
    reason: str | None,
) -> None:
    await record_event(
        db,
        device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "caller": "intent_reconciler",
            "reason": reason,
        },
    )
