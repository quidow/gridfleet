from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.device_event import DeviceEventType
from app.models.session import Session, SessionStatus
from app.models.test_run import TERMINAL_STATES
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services import lifecycle_incident_service, maintenance_service, run_service
from app.services.device_event_service import record_event
from app.services.lifecycle_policy_state import set_action, write_state

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device_reservation import DeviceReservation
    from app.models.test_run import TestRun
    from app.type_defs import NodeManagerResolver


async def has_running_client_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    stmt = (
        select(func.count())
        .select_from(Session)
        .where(
            Session.device_id == device_id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
    )
    result = await db.execute(stmt)
    return bool(result.scalar_one())


def failure_event_type(source: str) -> DeviceEventType:
    return DeviceEventType.connectivity_lost if source == "connectivity" else DeviceEventType.health_check_fail


def offline_summary_state(device: Device) -> DeviceLifecyclePolicySummaryState:
    if device.auto_manage:
        return DeviceLifecyclePolicySummaryState.recoverable
    return DeviceLifecyclePolicySummaryState.manual


def auto_stopped_summary_state(
    device: Device,
    run: TestRun | None,
) -> DeviceLifecyclePolicySummaryState:
    if run is not None:
        return DeviceLifecyclePolicySummaryState.excluded
    return offline_summary_state(device)


async def exclude_run_if_needed(
    db: AsyncSession,
    device: Device,
    *,
    reason: str,
    source: str,
) -> tuple[TestRun | None, DeviceReservation | None]:
    run, entry = await run_service.get_device_reservation_with_entry(db, device.id)
    if run is None:
        return None, entry

    was_excluded = run_service.reservation_entry_is_excluded(entry)
    run = await run_service.exclude_device_from_run(db, device.id, reason=reason, commit=False)
    entry = run_service.get_reservation_entry_for_device(run, device.id) if run is not None else None
    if run is not None and not was_excluded:
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_run_excluded,
            summary_state=DeviceLifecyclePolicySummaryState.excluded,
            reason=reason,
            detail=f"Excluded from {run.name}",
            source=source,
            run_id=run.id,
            run_name=run.name,
        )
        if "appium_node" not in device.__dict__:
            await db.refresh(device, ["appium_node"])
        await maintenance_service.enter_maintenance(db, device, drain=False, commit=False)
    return run, entry


async def restore_run_if_needed(
    db: AsyncSession,
    device: Device,
    run: TestRun | None,
    entry: DeviceReservation | None,
    *,
    reason: str,
    source: str,
) -> tuple[TestRun | None, DeviceReservation | None]:
    if run is None or run.state in TERMINAL_STATES or not run_service.reservation_entry_is_excluded(entry):
        return run, entry

    run = await run_service.restore_device_to_run(db, device.id, commit=False)
    entry = run_service.get_reservation_entry_for_device(run, device.id) if run is not None else None
    if run is not None:
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_run_restored,
            summary_state=DeviceLifecyclePolicySummaryState.idle,
            reason=reason,
            detail=f"Restored to {run.name}",
            source=source,
            run_id=run.id,
            run_name=run.name,
        )
    return run, entry


async def stop_node_and_mark_offline(
    db: AsyncSession,
    device: Device,
    *,
    source: str,
    reason: str,
    manager_resolver: NodeManagerResolver,
) -> None:
    await record_event(
        db,
        device.id,
        failure_event_type(source),
        {"source": source, "reason": reason},
    )
    await record_event(
        db,
        device.id,
        DeviceEventType.node_crash,
        {"error": reason, "source": source, "will_restart": bool(device.auto_manage)},
    )

    node = device.__dict__.get("appium_node")
    if node is not None and node.state == NodeState.running:
        try:
            manager = manager_resolver(device)
            await manager.stop_node(db, device)
        except Exception:
            device.availability_status = DeviceAvailabilityStatus.offline
            if node is not None:
                node.state = NodeState.error
                node.pid = None
            await db.commit()
    else:
        device.availability_status = DeviceAvailabilityStatus.offline
        if node is not None and node.state == NodeState.running:
            node.state = NodeState.stopped
            node.pid = None
        await db.commit()


async def record_recovery_suppressed(
    db: AsyncSession,
    device: Device,
    next_state: dict[str, Any],
    *,
    source: str,
    reason: str,
    suppression_reason: str,
    run: TestRun | None,
) -> bool:
    next_state["recovery_suppressed_reason"] = suppression_reason
    set_action(next_state, "recovery_suppressed")
    write_state(device, next_state)
    await lifecycle_incident_service.record_lifecycle_incident(
        db,
        device,
        DeviceEventType.lifecycle_recovery_suppressed,
        summary_state=DeviceLifecyclePolicySummaryState.suppressed,
        reason=suppression_reason,
        detail=reason,
        source=source,
        run_id=run.id if run is not None else None,
        run_name=run.name if run is not None else None,
    )
    await db.commit()
    return False


async def record_auto_stopped_incident(
    db: AsyncSession,
    device: Device,
    next_state: dict[str, Any],
    *,
    run: TestRun | None,
    reason: str,
    source: str,
    detail: str,
) -> None:
    set_action(next_state, "auto_stopped")
    write_state(device, next_state)
    await lifecycle_incident_service.record_lifecycle_incident(
        db,
        device,
        DeviceEventType.lifecycle_auto_stopped,
        summary_state=auto_stopped_summary_state(device, run),
        reason=reason,
        detail=detail,
        source=source,
        run_id=run.id if run is not None else None,
        run_name=run.name if run is not None else None,
    )


async def complete_auto_stop(
    db: AsyncSession,
    device: Device,
    next_state: dict[str, Any],
    *,
    reason: str,
    source: str,
    detail: str,
    manager_resolver: NodeManagerResolver,
) -> tuple[TestRun | None, DeviceReservation | None]:
    run, entry = await exclude_run_if_needed(db, device, reason=reason, source=source)
    await stop_node_and_mark_offline(
        db,
        device,
        source=source,
        reason=reason,
        manager_resolver=manager_resolver,
    )
    next_state["stop_pending"] = False
    next_state["stop_pending_reason"] = None
    next_state["stop_pending_since"] = None
    await record_auto_stopped_incident(
        db,
        device,
        next_state,
        run=run,
        reason=reason,
        source=source,
        detail=detail,
    )
    await db.commit()
    return run, entry
