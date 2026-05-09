from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceOperationalState
from app.models.device_event import DeviceEventType
from app.models.session import Session, SessionStatus
from app.models.test_run import TERMINAL_STATES
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services import (
    appium_node_locking,
    device_locking,
    lifecycle_incident_service,
    run_reservation_service,
)
from app.services.device_event_service import record_event
from app.services.device_state import set_operational_state
from app.services.event_bus import queue_device_crashed_event
from app.services.lifecycle_policy_state import (
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_backoff,
    clear_deferred_stop,
    set_action,
    write_state,
)
from app.services.lifecycle_policy_state import state as policy_state
from app.services.node_service import stop_node as stop_managed_node

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device_reservation import DeviceReservation
    from app.models.test_run import TestRun


async def _lock_for_state_write(db: AsyncSession, device: Device) -> Device:
    return await device_locking.lock_device(db, device.id, load_sessions=True)


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
    """Exclude the device from its active run reservation and emit the
    ``lifecycle_run_excluded`` incident.

    Does NOT escalate the device into maintenance. Auto-escalation to
    maintenance from health/connectivity failures is intentionally absent —
    only three paths are allowed to flip ``hold`` to ``maintenance``:
    operator-driven UI actions, ``report_preparation_failure`` (testkit
    pre-run signal), and ``release_claimed_device_with_cooldown`` after the
    cooldown threshold is exceeded. Anything else is treated as a regression.
    Callers that need the device parked in maintenance must call
    ``maintenance_service.enter_maintenance`` themselves.
    """
    run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
    if run is None:
        return None, entry

    was_excluded = run_reservation_service.reservation_entry_is_excluded(entry)
    run = await run_reservation_service.exclude_device_from_run(db, device.id, reason=reason, commit=False)
    entry = run_reservation_service.get_reservation_entry_for_device(run, device.id) if run is not None else None
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
    if run is None or run.state in TERMINAL_STATES or not run_reservation_service.reservation_entry_is_excluded(entry):
        return run, entry

    run = await run_reservation_service.restore_device_to_run(db, device.id, commit=False)
    entry = run_reservation_service.get_reservation_entry_for_device(run, device.id) if run is not None else None
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


async def handle_node_crash(
    db: AsyncSession,
    device: Device,
    *,
    source: str,
    reason: str,
) -> None:
    """Record a node crash and stop the underlying Appium node.

    Triggered by ``complete_auto_stop`` on ``connectivity_lost`` and
    ``health_check_fail`` in addition to genuine Appium crashes — every
    invocation persists a ``node_crash`` event unconditionally.

    Operational-state semantics (three distinct paths):
    - Node running + ``stop_managed_node`` succeeds: operational state delegates
      to ``mark_node_stopped`` and hold is preserved independently.
    - Node running + ``stop_managed_node`` raises: re-acquires both row locks
      (Device → AppiumNode, documented order) before forcing ``offline`` and
      setting ``node.state = NodeState.error``.
    - Node not running or absent (``else`` branch): forces ``offline`` directly
      using the already-held row lock; no re-acquisition needed.

    ``record_event(DeviceEventType.node_crash, ...)`` fires on every branch —
    webhook subscribers of ``device.crashed`` see the crash unconditionally.
    """
    device = await _lock_for_state_write(db, device)
    node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
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
    queue_device_crashed_event(
        db,
        device_id=str(device.id),
        device_name=device.name,
        source=source,
        reason=reason,
        will_restart=bool(device.auto_manage),
        process=None,
    )

    if node is not None and node.state == NodeState.running:
        try:
            await stop_managed_node(db, device)
        except Exception:
            # stop_managed_node may commit before raising, releasing both row locks.
            # Re-acquire in the documented Device -> AppiumNode order before
            # writing offline/error state.
            device = await device_locking.lock_device(db, device.id, load_sessions=True)
            locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
            await set_operational_state(
                device,
                DeviceOperationalState.offline,
                reason=f"Node crash recorded ({source}): {reason}",
            )
            if locked_node is not None:
                locked_node.state = NodeState.error
                locked_node.pid = None
            await db.commit()
    else:
        await set_operational_state(
            device,
            DeviceOperationalState.offline,
            reason=f"Node crash recorded ({source}): {reason}",
        )
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
    device = await _lock_for_state_write(db, device)
    # Re-derive the working state from the freshly-locked device so that
    # fields written by concurrent committers (between our caller's read and
    # our write) are not clobbered.  The caller is expected to either persist
    # its intent eagerly (when an intermediate commit may release the row lock,
    # e.g. handle_health_failure → complete_auto_stop) or hold the lock
    # continuously through to this call (e.g. attempt_auto_recovery).
    # Either way, by the time we re-lock here the row reflects the caller's
    # intent plus any concurrent committers' updates.
    fresh = policy_state(device)
    fresh["recovery_suppressed_reason"] = suppression_reason
    set_action(fresh, "recovery_suppressed")
    write_state(device, fresh)
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
    device = await _lock_for_state_write(db, device)
    # Preserve any state mutations committed by concurrent writers between
    # our caller's read and our write.  ``stop_pending*`` is the only field
    # this call site explicitly resets, so we carry that forward from
    # ``next_state`` (the caller already set it to ``False``).
    fresh = policy_state(device)
    fresh["stop_pending"] = next_state.get("stop_pending", False)
    fresh["stop_pending_reason"] = next_state.get("stop_pending_reason")
    fresh["stop_pending_since"] = next_state.get("stop_pending_since")
    set_action(fresh, "auto_stopped")
    write_state(device, fresh)
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
) -> tuple[TestRun | None, DeviceReservation | None]:
    device = await _lock_for_state_write(db, device)
    run, entry = await exclude_run_if_needed(db, device, reason=reason, source=source)
    await handle_node_crash(
        db,
        device,
        source=source,
        reason=reason,
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


async def record_ci_preparation_failed(
    db: AsyncSession,
    device: Device,
    *,
    reason: str,
    source: str,
) -> None:
    """Persist the CI preparation failure onto the lifecycle_policy_state JSON.

    The caller is responsible for holding the device row lock and for the
    surrounding maintenance / event / commit work; this helper only owns the
    JSON column write so ``run_service`` does not need to import
    ``write_state`` or other low-level lifecycle_policy_state primitives.
    """
    device = await _lock_for_state_write(db, device)
    fresh = policy_state(device)
    fresh["last_failure_source"] = source
    fresh["last_failure_reason"] = reason
    clear_deferred_stop(fresh)
    fresh["recovery_suppressed_reason"] = MAINTENANCE_HOLD_SUPPRESSION_REASON
    clear_backoff(fresh)
    set_action(fresh, "ci_preparation_failed")
    write_state(device, fresh)
