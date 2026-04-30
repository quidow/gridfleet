from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.device_event import DeviceEventType
from app.models.test_run import TERMINAL_STATES
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services import lifecycle_incident_service, lifecycle_policy_summary, run_service
from app.services.device_event_service import record_event
from app.services.device_readiness import is_ready_for_use_async
from app.services.lifecycle_policy_actions import (
    complete_auto_stop,
    exclude_run_if_needed,
    has_running_client_session,
    record_auto_stopped_incident,
    record_recovery_suppressed,
    restore_run_if_needed,
)
from app.services.lifecycle_policy_state import (
    clear_backoff,
    loaded_node,
    now,
    now_iso,
    parse_iso,
    set_action,
    write_state,
)
from app.services.lifecycle_policy_state import (
    set_backoff as _set_backoff_with_settings,
)
from app.services.lifecycle_policy_state import (
    state as policy_state,
)
from app.services.node_manager import get_node_manager
from app.services.node_manager_types import NodeManagerError
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

build_lifecycle_policy = lifecycle_policy_summary.build_lifecycle_policy
build_lifecycle_policy_summary = lifecycle_policy_summary.build_lifecycle_policy_summary

RECOVERY_PROBE_ATTEMPTS = 3
RECOVERY_PROBE_RETRY_DELAY_SEC = 10


def _set_backoff(state: dict[str, Any]) -> str:
    base_seconds = int(settings_service.get("general.lifecycle_recovery_backoff_base_sec"))
    max_seconds = max(base_seconds, int(settings_service.get("general.lifecycle_recovery_backoff_max_sec")))
    return _set_backoff_with_settings(state, base_seconds=base_seconds, max_seconds=max_seconds)


def record_control_action(
    device: Device,
    *,
    action: str,
    failure_source: str | None = None,
    failure_reason: str | None = None,
    recovery_suppressed_reason: str | None = None,
) -> None:
    current_state = policy_state(device)
    if failure_source is not None:
        current_state["last_failure_source"] = failure_source
    if failure_reason is not None:
        current_state["last_failure_reason"] = failure_reason
    current_state["recovery_suppressed_reason"] = recovery_suppressed_reason
    set_action(current_state, action)
    write_state(device, current_state)


async def _reload_device(db: AsyncSession, device: Device) -> Device:
    stmt = (
        select(Device)
        .where(Device.id == device.id)
        .options(selectinload(Device.appium_node), selectinload(Device.sessions), selectinload(Device.host))
        .execution_options(populate_existing=True)
    )
    refreshed = await db.scalar(stmt)
    return refreshed or device


async def handle_health_failure(
    db: AsyncSession,
    device: Device,
    *,
    source: str,
    reason: str,
) -> str:
    current_state = policy_state(device)
    current_state["last_failure_source"] = source
    current_state["last_failure_reason"] = reason
    current_state["recovery_suppressed_reason"] = None

    if device.availability_status == DeviceAvailabilityStatus.maintenance:
        await record_recovery_suppressed(
            db,
            device,
            current_state,
            source=source,
            reason=reason,
            suppression_reason="Device is in maintenance mode",
            run=None,
        )
        return "suppressed"

    if await has_running_client_session(db, device.id):
        current_state["stop_pending"] = True
        current_state["stop_pending_reason"] = reason
        current_state["stop_pending_since"] = now_iso()
        set_action(current_state, "auto_stop_deferred")
        write_state(device, current_state)
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_deferred_stop,
            summary_state=DeviceLifecyclePolicySummaryState.deferred_stop,
            reason=reason,
            detail="Waiting for the active client session to finish",
            source=source,
        )
        await db.commit()
        return "deferred"

    await complete_auto_stop(
        db,
        device,
        current_state,
        reason=reason,
        source=source,
        detail="Manager stopped the device automatically after a lifecycle failure",
        manager_resolver=get_node_manager,
    )
    return "stopped"


async def handle_session_finished(db: AsyncSession, device: Device) -> bool:
    current_state = policy_state(device)
    if not current_state.get("stop_pending"):
        return False

    reason = (
        current_state.get("stop_pending_reason")
        or current_state.get("last_failure_reason")
        or "Health-driven stop pending"
    )
    source = current_state.get("last_failure_source") or "device_checks"
    await complete_auto_stop(
        db,
        device,
        current_state,
        reason=reason,
        source=source,
        detail="Manager completed a previously deferred automatic stop",
        manager_resolver=get_node_manager,
    )
    return True


async def note_connectivity_loss(
    db: AsyncSession,
    device: Device,
    *,
    reason: str,
) -> None:
    current_state = policy_state(device)
    current_state["last_failure_source"] = "connectivity"
    current_state["last_failure_reason"] = reason
    current_state["stop_pending"] = False
    current_state["stop_pending_reason"] = None
    current_state["stop_pending_since"] = None

    run, _entry = await exclude_run_if_needed(db, device, reason=reason, source="connectivity")
    await record_auto_stopped_incident(
        db,
        device,
        current_state,
        run=run,
        reason=reason,
        source="connectivity",
        detail="Manager marked the device offline after connectivity loss",
    )


async def attempt_auto_recovery(
    db: AsyncSession,
    device: Device,
    *,
    source: str,
    reason: str,
) -> bool:
    device = await _reload_device(db, device)
    current_state = policy_state(device)
    run, entry = await run_service.get_device_reservation_with_entry(db, device.id)
    node = loaded_node(device)
    if (
        node is not None
        and node.state == NodeState.running
        and device.availability_status != DeviceAvailabilityStatus.offline
        and not run_service.reservation_entry_is_excluded(entry)
    ):
        return False

    if not device.auto_manage:
        return await record_recovery_suppressed(
            db,
            device,
            current_state,
            source=source,
            reason=reason,
            suppression_reason="Auto-manage is disabled",
            run=run,
        )
    if not await is_ready_for_use_async(db, device):
        return await record_recovery_suppressed(
            db,
            device,
            current_state,
            source=source,
            reason=reason,
            suppression_reason="Device setup or verification is incomplete",
            run=run,
        )
    if device.availability_status == DeviceAvailabilityStatus.maintenance:
        return await record_recovery_suppressed(
            db,
            device,
            current_state,
            source=source,
            reason=reason,
            suppression_reason="Device is in maintenance mode",
            run=run,
        )
    if current_state.get("stop_pending"):
        return await record_recovery_suppressed(
            db,
            device,
            current_state,
            source=source,
            reason=reason,
            suppression_reason="Waiting for active client session to finish",
            run=run,
        )
    if await has_running_client_session(db, device.id):
        return await record_recovery_suppressed(
            db,
            device,
            current_state,
            source=source,
            reason=reason,
            suppression_reason="A client session is still running",
            run=run,
        )

    backoff_until = parse_iso(current_state.get("backoff_until"))
    if backoff_until is not None and backoff_until > now():
        current_state["recovery_suppressed_reason"] = f"Backing off until {backoff_until.isoformat()}"
        set_action(current_state, "recovery_suppressed")
        write_state(device, current_state)
        await db.commit()
        return False

    current_state["recovery_suppressed_reason"] = None
    set_action(current_state, "recovery_started")
    write_state(device, current_state)

    node = loaded_node(device)
    if node is None or node.state != NodeState.running:
        try:
            manager = get_node_manager(device)
            await manager.start_node(db, device)
        except NodeManagerError as exc:
            current_state["last_failure_source"] = source
            current_state["last_failure_reason"] = str(exc)
            current_state["recovery_suppressed_reason"] = "Automatic restart failed"
            backoff_until_iso = _set_backoff(current_state)
            set_action(current_state, "recovery_failed")
            write_state(device, current_state)
            await lifecycle_incident_service.record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovery_failed,
                summary_state=DeviceLifecyclePolicySummaryState.backoff,
                reason=str(exc),
                detail=current_state["recovery_suppressed_reason"],
                source=source,
                run_id=run.id if run is not None else None,
                run_name=run.name if run is not None else None,
                backoff_until=backoff_until_iso,
            )
            await lifecycle_incident_service.record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovery_backoff,
                summary_state=DeviceLifecyclePolicySummaryState.backoff,
                reason=str(exc),
                detail="Automatic recovery is backing off before the next retry",
                source=source,
                run_id=run.id if run is not None else None,
                run_name=run.name if run is not None else None,
                backoff_until=backoff_until_iso,
            )
            await db.commit()
            return False

    from app.services import session_viability

    result: dict[str, Any] = {}
    for attempt in range(max(1, RECOVERY_PROBE_ATTEMPTS)):
        device = await _reload_device(db, device)
        result = await session_viability.run_session_viability_probe(db, device, checked_by="recovery")
        if result.get("status") == "passed":
            break
        if attempt + 1 < RECOVERY_PROBE_ATTEMPTS:
            await asyncio.sleep(RECOVERY_PROBE_RETRY_DELAY_SEC)

    if result.get("status") != "passed":
        failure_reason = result.get("error") or "Recovery viability probe failed"
        current_state["last_failure_source"] = "session_viability"
        current_state["last_failure_reason"] = failure_reason
        current_state["recovery_suppressed_reason"] = "Recovery probe failed"
        backoff_until_iso = _set_backoff(current_state)
        await complete_auto_stop(
            db,
            device,
            current_state,
            reason=failure_reason,
            source="session_viability",
            detail="Manager stopped the device after a failed recovery viability probe",
            manager_resolver=get_node_manager,
        )
        run, entry = await run_service.get_device_reservation_with_entry(db, device.id)
        set_action(current_state, "recovery_failed")
        write_state(device, current_state)
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovery_failed,
            summary_state=DeviceLifecyclePolicySummaryState.backoff,
            reason=failure_reason,
            detail=current_state["recovery_suppressed_reason"],
            source="session_viability",
            run_id=run.id if run is not None else None,
            run_name=run.name if run is not None else None,
            backoff_until=backoff_until_iso,
        )
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovery_backoff,
            summary_state=DeviceLifecyclePolicySummaryState.backoff,
            reason=failure_reason,
            detail="Automatic recovery is backing off before the next retry",
            source="session_viability",
            run_id=run.id if run is not None else None,
            run_name=run.name if run is not None else None,
            backoff_until=backoff_until_iso,
        )
        await db.commit()
        return False

    clear_backoff(current_state)
    current_state["recovery_suppressed_reason"] = None

    if run is not None and run.state not in TERMINAL_STATES:
        if run_service.reservation_entry_is_excluded(entry):
            run, entry = await restore_run_if_needed(
                db,
                device,
                run,
                entry,
                reason=reason,
                source=source,
            )
            await record_event(
                db,
                device.id,
                DeviceEventType.node_restart,
                {"recovered_from": source, "reason": reason},
            )
            device.availability_status = DeviceAvailabilityStatus.reserved
            await db.commit()
        else:
            device.availability_status = DeviceAvailabilityStatus.reserved
            await db.commit()
    else:
        await record_event(
            db,
            device.id,
            DeviceEventType.node_restart,
            {"recovered_from": source, "reason": reason},
        )
        if device.availability_status != DeviceAvailabilityStatus.available:
            if await is_ready_for_use_async(db, device):
                device.availability_status = DeviceAvailabilityStatus.available
            else:
                device.availability_status = DeviceAvailabilityStatus.offline
        await db.commit()

    await record_event(
        db,
        device.id,
        DeviceEventType.connectivity_restored,
        {"source": source, "reason": reason, "rejoined_run_id": str(run.id) if run else None},
    )
    await db.commit()

    set_action(current_state, "auto_recovered")
    write_state(device, current_state)
    await lifecycle_incident_service.record_lifecycle_incident(
        db,
        device,
        DeviceEventType.lifecycle_recovered,
        summary_state=DeviceLifecyclePolicySummaryState.idle,
        reason=reason,
        detail="Device recovered and rejoined automatic management",
        source=source,
        run_id=run.id if run is not None else None,
        run_name=run.name if run is not None else None,
    )
    await db.commit()
    return True
