"""Single chokepoint for Phase 3 Appium desired-state writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from sqlalchemy import desc, select

from app import metrics_recorders
from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_event import DeviceEvent, DeviceEventType
from app.observability import get_logger
from app.services.device_event_service import record_event

logger = get_logger(__name__)

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

DesiredStateCaller = Literal[
    "operator_route",
    "operator_restart",
    "lifecycle_recovery",
    "lifecycle_crash",
    "connectivity",
    "health_restart",
    "maintenance_enter",
    "maintenance_exit",
    "bulk",
    "group",
    "verification",
    "device_delete",
    "admin_clear_transition",
    "appium_reconciler",
    "cooldown",
    "cooldown_expired",
    "intent_reconciler",
]

DesiredGridRunIdCaller = Literal[
    "run_create",
    "run_complete",
    "run_cancel",
    "run_force_release",
    "run_expire",
    "run_preparation_failed",
    "run_exclude_device",
    "run_restore_device",
    "reservation_backfill",
    "intent_reconciler",
]


async def write_desired_state(
    db: AsyncSession,
    *,
    node: AppiumNode,
    target: AppiumDesiredState,
    caller: DesiredStateCaller,
    desired_port: int | None = None,
    transition_token: uuid.UUID | None = None,
    transition_deadline: datetime | None = None,
    actor: str | None = None,
    reason: str | None = None,
) -> None:
    """Write desired Appium state on an already locked node row. Caller commits."""
    if target == AppiumDesiredState.stopped and desired_port is not None:
        raise ValueError("desired_port must be None when target=stopped")
    if transition_token is not None and transition_deadline is None:
        raise ValueError("transition_deadline is required when transition_token is set")

    old_state = node.desired_state
    old_token = node.transition_token
    old_port = node.desired_port

    new_port = None if target == AppiumDesiredState.stopped else desired_port
    if old_state == target and old_token == transition_token and old_port == new_port:
        return

    if old_token is not None and old_token != transition_token:
        losing_source = await _lookup_token_source(db, node.device_id, old_token)
        metrics_recorders.APPIUM_TRANSITION_TOKEN_OVERRIDDEN.labels(
            losing_source=losing_source,
            winning_source=caller,
        ).inc()
        logger.warning(
            "appium_transition_token_overridden",
            device_id=str(node.device_id),
            losing_source=losing_source,
            winning_source=caller,
            old_token=str(old_token),
            new_token=str(transition_token) if transition_token else None,
        )

    node.desired_state = target
    if target == AppiumDesiredState.stopped:
        node.desired_port = None
        node.transition_token = None
        node.transition_deadline = None
        event_desired_port = None
    else:
        node.desired_port = desired_port
        node.transition_token = transition_token
        node.transition_deadline = transition_deadline
        event_desired_port = desired_port

    await record_event(
        db,
        node.device_id,
        DeviceEventType.desired_state_changed,
        {
            "old_desired_state": old_state.value,
            "new_desired_state": target.value,
            "desired_port": event_desired_port,
            "transition_token": str(transition_token) if transition_token else None,
            "caller": caller,
            "actor": actor,
            "reason": reason,
        },
    )

    metrics_recorders.APPIUM_DESIRED_STATE_WRITES.labels(caller=caller, target_state=target.value).inc()
    if transition_token is not None:
        metrics_recorders.APPIUM_TRANSITION_TOKEN_WRITES.labels(caller=caller).inc()

    logger.info(
        "appium_desired_state_written",
        device_id=str(node.device_id),
        caller=caller,
        old_desired_state=old_state.value,
        new_desired_state=target.value,
        desired_port=event_desired_port,
        transition_token=str(transition_token) if transition_token else None,
    )


async def write_desired_grid_run_id(
    db: AsyncSession,
    *,
    node: AppiumNode,
    run_id: uuid.UUID | None,
    caller: DesiredGridRunIdCaller,
    actor: str | None = None,
    reason: str | None = None,
) -> None:
    """Write desired Grid routing run id on an already locked node row. Caller commits."""
    old = node.desired_grid_run_id
    if old == run_id:
        return

    node.desired_grid_run_id = run_id

    await record_event(
        db,
        node.device_id,
        DeviceEventType.desired_state_changed,
        {
            "field": "desired_grid_run_id",
            "old_value": str(old) if old else None,
            "new_value": str(run_id) if run_id else None,
            "caller": caller,
            "actor": actor,
            "reason": reason,
        },
    )

    metrics_recorders.APPIUM_DESIRED_GRID_RUN_ID_WRITES.labels(caller=caller).inc()
    logger.info(
        "appium_desired_grid_run_id_written",
        device_id=str(node.device_id),
        caller=caller,
        old_value=str(old) if old else None,
        new_value=str(run_id) if run_id else None,
    )


async def _lookup_token_source(db: AsyncSession, device_id: uuid.UUID, token: uuid.UUID) -> str:
    stmt = (
        select(DeviceEvent)
        .where(
            DeviceEvent.device_id == device_id,
            DeviceEvent.event_type == DeviceEventType.desired_state_changed,
        )
        .order_by(desc(DeviceEvent.created_at))
        .limit(50)
    )
    rows = (await db.execute(stmt)).scalars().all()
    for row in rows:
        details = row.details or {}
        if details.get("transition_token") == str(token):
            caller = details.get("caller")
            if isinstance(caller, str):
                return caller
    return "unknown"
