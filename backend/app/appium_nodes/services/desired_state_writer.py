"""Single chokepoint for Phase 3 Appium desired-state writes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core import metrics_recorders
from app.core.observability import get_logger
from app.devices.models import DeviceEventType
from app.devices.services.event import record_event

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


@dataclass(frozen=True, slots=True)
class DesiredStateWrite:
    """Cohesive payload describing a single desired Appium state write.

    Bundles the target state with its restart watermark and audit metadata so
    the writer call site stays under the argument-count ceiling. ``db``,
    ``node`` and ``caller`` remain direct arguments to ``write_desired_state``
    because they describe *where* the write lands and *who* issued it, not
    *what* is being written.

    """

    target: AppiumDesiredState
    desired_port: int | None = None
    restart_requested_at: datetime | None = None
    actor: str | None = None
    reason: str | None = None


async def write_desired_state(
    db: AsyncSession,
    *,
    node: AppiumNode,
    caller: DesiredStateCaller,
    write: DesiredStateWrite,
) -> None:
    """Write desired Appium state on an already locked node row. Caller commits."""
    target = write.target
    desired_port = write.desired_port
    restart_requested_at = write.restart_requested_at
    actor = write.actor
    reason = write.reason

    if target == AppiumDesiredState.stopped and desired_port is not None:
        raise ValueError("desired_port must be None when target=stopped")

    old_state = node.desired_state
    old_restart_requested_at = node.restart_requested_at
    old_port = node.desired_port

    new_port = None if target == AppiumDesiredState.stopped else desired_port
    new_restart_requested_at = None if target == AppiumDesiredState.stopped else restart_requested_at
    if old_state == target and old_restart_requested_at == new_restart_requested_at and old_port == new_port:
        return

    node.desired_state = target
    if target == AppiumDesiredState.stopped:
        node.desired_port = None
        node.restart_requested_at = None
        event_desired_port = None
    else:
        node.desired_port = desired_port
        node.restart_requested_at = restart_requested_at
        event_desired_port = desired_port

    await record_event(
        db,
        node.device_id,
        DeviceEventType.desired_state_changed,
        {
            "old_desired_state": old_state.value,
            "new_desired_state": target.value,
            "desired_port": event_desired_port,
            "restart_requested_at": restart_requested_at.isoformat() if restart_requested_at else None,
            "caller": caller,
            "actor": actor,
            "reason": reason,
        },
    )

    metrics_recorders.APPIUM_DESIRED_STATE_WRITES.labels(caller=caller, target_state=target.value).inc()

    logger.info(
        "appium_desired_state_written",
        device_id=str(node.device_id),
        caller=caller,
        old_desired_state=old_state.value,
        new_desired_state=target.value,
        desired_port=event_desired_port,
        restart_requested_at=restart_requested_at.isoformat() if restart_requested_at else None,
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
