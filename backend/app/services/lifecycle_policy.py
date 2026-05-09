from __future__ import annotations

import asyncio
import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.models.appium_node import NodeState
from app.models.device import Device, DeviceHold, DeviceOperationalState
from app.models.device_event import DeviceEventType
from app.models.test_run import TERMINAL_STATES
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services import (
    device_health,
    device_locking,
    lifecycle_incident_service,
    lifecycle_policy_summary,
    run_reservation_service,
    session_viability,
)
from app.services.device_event_service import record_event
from app.services.device_readiness import is_ready_for_use_async
from app.services.device_state import ready_operational_state, set_hold, set_operational_state
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
    clear_deferred_stop,
    loaded_node,
    now,
    parse_iso,
    record_backoff_suppressed,
    record_maintenance_exited,
    record_recovery_failed,
    record_recovery_recovered,
    record_recovery_started,
    set_action,
    set_deferred_stop,
    write_state,
)
from app.services.lifecycle_policy_state import (
    set_backoff as _set_backoff_with_settings,
)
from app.services.lifecycle_policy_state import (
    state as policy_state,
)
from app.services.node_service import start_node as start_managed_node
from app.services.node_service_types import NodeManagerError
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

build_lifecycle_policy = lifecycle_policy_summary.build_lifecycle_policy
build_lifecycle_policy_summary = lifecycle_policy_summary.build_lifecycle_policy_summary

RECOVERY_PROBE_ATTEMPTS = 3
RECOVERY_PROBE_RETRY_DELAY_SEC = 10


class DeferredStopOutcome(StrEnum):
    """Outcome of ``complete_deferred_stop_if_session_ended`` /
    ``handle_session_finished``.

    NO_PENDING: device had no ``stop_pending`` intent.
    RUNNING_SESSION_EXISTS: another client session is still running, so the
        helper bailed without touching state.
    CLEARED_RECOVERED: device became healthy before the session ended; the
        intent was cleared and no auto-stop was performed. The caller should
        restore device availability the same way it would for any session-end
        on a healthy device.
    AUTO_STOPPED: the deferred stop was completed; the device is offline (and
        excluded from any active run).
    """

    NO_PENDING = "no_pending"
    RUNNING_SESSION_EXISTS = "running_session_exists"
    CLEARED_RECOVERED = "cleared_recovered"
    AUTO_STOPPED = "auto_stopped"


def _set_backoff(state: dict[str, Any]) -> str:
    base_seconds = int(settings_service.get("general.lifecycle_recovery_backoff_base_sec"))
    max_seconds = max(base_seconds, int(settings_service.get("general.lifecycle_recovery_backoff_max_sec")))
    return _set_backoff_with_settings(state, base_seconds=base_seconds, max_seconds=max_seconds)


async def record_control_action(
    db: AsyncSession,
    device: Device,
    *,
    action: str,
    failure_source: str | None = None,
    failure_reason: str | None = None,
    recovery_suppressed_reason: str | None = None,
) -> None:
    device = await _reload_device(db, device)
    current_state = policy_state(device)
    if failure_source is not None:
        current_state["last_failure_source"] = failure_source
    if failure_reason is not None:
        current_state["last_failure_reason"] = failure_reason
    current_state["recovery_suppressed_reason"] = recovery_suppressed_reason
    set_action(current_state, action)
    write_state(device, current_state)


async def clear_pending_auto_stop_on_recovery(
    db: AsyncSession,
    device: Device,
    *,
    source: str,
    reason: str,
    action: str | None = None,
    record_incident: bool = True,
) -> bool:
    """Drop a previously-queued deferred auto-stop because health recovered.

    Returns True when an intent was actually cleared, False when nothing was
    pending. Caller is responsible for committing.

    ``action`` refreshes ``last_action`` so callers that did not invoke
    ``record_control_action`` ahead of time still leave an accurate trail
    instead of a stale ``auto_stop_deferred``.

    ``record_incident`` controls whether a ``lifecycle_recovered`` incident
    is emitted. Callers that already publish their own dedicated recovery
    incident (e.g. ``node_health``) pass ``False`` to avoid duplicates.
    """
    device = await _reload_device(db, device)
    current_state = policy_state(device)
    if not current_state.get("stop_pending"):
        return False

    pending_since = current_state.get("stop_pending_since")
    pending_reason = current_state.get("stop_pending_reason")
    clear_deferred_stop(current_state)
    if action is not None:
        set_action(current_state, action)
    write_state(device, current_state)

    if record_incident:
        detail = (
            f"Recovery cleared deferred stop queued at {pending_since}"
            if pending_since
            else "Recovery cleared deferred stop"
        )
        if pending_reason:
            detail = f"{detail} (was: {pending_reason})"

        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovered,
            summary_state=DeviceLifecyclePolicySummaryState.idle,
            reason=reason,
            detail=detail,
            source=source,
        )
    return True


async def _reload_device(db: AsyncSession, device: Device) -> Device:
    return await device_locking.lock_device(db, device.id, load_sessions=True)


async def handle_health_failure(
    db: AsyncSession,
    device: Device,
    *,
    source: str,
    reason: str,
) -> str:
    device = await _reload_device(db, device)
    current_state = policy_state(device)
    current_state["last_failure_source"] = source
    current_state["last_failure_reason"] = reason
    current_state["recovery_suppressed_reason"] = None
    # Persist this writer's intent into the session's identity map immediately
    # so the next intermediate commit lands these fields in the DB. Otherwise
    # downstream helpers that re-lock + refresh would lose this writer's intent
    # (see Task 9 / R4 — lock device row across lifecycle_policy_state RMW).
    write_state(device, current_state)

    if device.hold == DeviceHold.maintenance:
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
        set_deferred_stop(current_state, reason=reason)
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
    )
    return "stopped"


async def handle_session_finished(db: AsyncSession, device: Device) -> DeferredStopOutcome:
    device = await _reload_device(db, device)
    current_state = policy_state(device)
    if not current_state.get("stop_pending"):
        return DeferredStopOutcome.NO_PENDING

    # Authoritative check under the device row lock. Callers may have
    # pre-validated outside the lock for early-exit, but a fresh client
    # session can start between that check and the lock; only the locked
    # check is safe.
    if await has_running_client_session(db, device.id):
        return DeferredStopOutcome.RUNNING_SESSION_EXISTS

    summary = device_health.build_public_summary(device)
    node = loaded_node(device)
    node_running = node is not None and node.state == NodeState.running

    if summary.get("healthy") is True and node_running:
        # Defense in depth: ``clear_pending_auto_stop_on_recovery`` should
        # already have cleared the intent when health recovered. If anything
        # slipped the device into a healthy state without going through that
        # path, the row-derived projection is treated as the canonical health
        # source. A subsequent failed probe will re-enter
        # ``handle_health_failure``.
        await clear_pending_auto_stop_on_recovery(
            db,
            device,
            source=current_state.get("last_failure_source") or "session",
            reason="Session finished while device was healthy",
            action="auto_stop_cleared",
        )
        # Mirror the AUTO_STOPPED branch (which commits via ``complete_auto_stop``):
        # commit the cleared intent here so callers do not need to know about the
        # internal helper contract. Without this commit, request-scoped sessions
        # (FastAPI ``get_db``) close before the cleared state is persisted, and
        # the dashboard keeps rendering stale ``stop_pending``.
        await db.commit()
        return DeferredStopOutcome.CLEARED_RECOVERED

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
    )
    return DeferredStopOutcome.AUTO_STOPPED


async def complete_deferred_stop_if_session_ended(db: AsyncSession, device: Device) -> DeferredStopOutcome:
    """Idempotent session-end helper. Authoritative state checks live in
    ``handle_session_finished``, which re-reads under the device row lock —
    so callers do not need to (and must not) pre-validate state.
    """
    return await handle_session_finished(db, device)


async def note_connectivity_loss(
    db: AsyncSession,
    device: Device,
    *,
    reason: str,
) -> None:
    device = await _reload_device(db, device)
    current_state = policy_state(device)
    current_state["last_failure_source"] = "connectivity"
    current_state["last_failure_reason"] = reason
    clear_deferred_stop(current_state)
    # Persist intent before any await/commit (see handle_health_failure).
    write_state(device, current_state)

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
    run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
    node = loaded_node(device)
    if (
        node is not None
        and node.state == NodeState.running
        and device.operational_state != DeviceOperationalState.offline
        and not run_reservation_service.reservation_entry_is_excluded(entry)
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
    if device.hold == DeviceHold.maintenance:
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
        record_backoff_suppressed(current_state, until_iso=backoff_until.isoformat())
        write_state(device, current_state)
        await db.commit()
        return False

    record_recovery_started(current_state)
    write_state(device, current_state)

    node = loaded_node(device)
    if node is None or node.state != NodeState.running:
        try:
            await start_managed_node(db, device)
        except NodeManagerError as exc:
            backoff_until_iso = _set_backoff(current_state)
            record_recovery_failed(
                current_state,
                source=source,
                reason=str(exc),
                suppression_reason="Automatic restart failed",
            )
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
        backoff_until_iso = _set_backoff(current_state)
        record_recovery_failed(
            current_state,
            source="session_viability",
            reason=failure_reason,
            suppression_reason="Recovery probe failed",
        )
        write_state(device, current_state)  # eager-write before potential intermediate commit in complete_auto_stop
        await complete_auto_stop(
            db,
            device,
            current_state,
            reason=failure_reason,
            source="session_viability",
            detail="Manager stopped the device after a failed recovery viability probe",
        )

        # Re-lock and rebuild state from fresh DB row: complete_auto_stop releases
        # the row lock via intermediate commits in handle_node_crash.
        # Without this re-lock, the trailing write_state below would clobber any
        # concurrent writer (e.g., note_connectivity_loss) on the same device.
        device = await device_locking.lock_device(db, device.id, load_sessions=True)
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        fresh_state = policy_state(device)
        fresh_state["backoff_until"] = backoff_until_iso
        fresh_state["recovery_backoff_attempts"] = current_state["recovery_backoff_attempts"]
        record_recovery_failed(
            fresh_state,
            source="session_viability",
            reason=failure_reason,
            suppression_reason="Recovery probe failed",
        )
        write_state(device, fresh_state)
        await lifecycle_incident_service.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovery_failed,
            summary_state=DeviceLifecyclePolicySummaryState.backoff,
            reason=failure_reason,
            detail=fresh_state["recovery_suppressed_reason"],
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

    # Re-lock and rebuild state from fresh DB row: run_session_viability_probe
    # commits multiple times during execution, releasing the row lock that
    # _reload_device acquired at the top of this function. Without this re-lock,
    # the trailing writes below would clobber any concurrent writer on the same device.
    device = await device_locking.lock_device(db, device.id, load_sessions=True)
    fresh_state = policy_state(device)
    # Carry forward this writer's intent in current_state into fresh_state, but
    # only the fields this branch will touch downstream:
    fresh_state["recovery_backoff_attempts"] = current_state.get("recovery_backoff_attempts", 0)
    current_state = fresh_state
    # Re-resolve the reservation under lock as well:
    run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)

    clear_backoff(current_state)
    current_state["recovery_suppressed_reason"] = None

    if run is not None and run.state not in TERMINAL_STATES:
        if run_reservation_service.reservation_entry_is_excluded(entry):
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
            await set_hold(
                device,
                DeviceHold.reserved,
                reason=f"Rejoined run after {source}: {reason}",
            )
            await db.commit()
        else:
            await set_hold(
                device,
                DeviceHold.reserved,
                reason=f"Rejoined run after {source}: {reason}",
            )
            await db.commit()
    else:
        await record_event(
            db,
            device.id,
            DeviceEventType.node_restart,
            {"recovered_from": source, "reason": reason},
        )
        if device.operational_state != DeviceOperationalState.available:
            await set_operational_state(
                device,
                await ready_operational_state(db, device),
                reason=f"Connectivity restored ({source}): {reason}",
            )
        await db.commit()

    await record_event(
        db,
        device.id,
        DeviceEventType.connectivity_restored,
        {"source": source, "reason": reason, "rejoined_run_id": str(run.id) if run else None},
    )
    await db.commit()

    # Re-lock for the trailing lifecycle write: the per-branch commits above
    # released the FOR UPDATE acquired earlier in this function.
    device = await device_locking.lock_device(db, device.id, load_sessions=True)
    fresh_state = policy_state(device)
    record_recovery_recovered(fresh_state)
    current_state = fresh_state
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


def clear_maintenance_recovery_suppression(device: Device) -> None:
    """Clear lifecycle suppression that ``handle_health_failure`` records when
    a device fails a probe while held in maintenance.

    The suppression reason ("Device is in maintenance mode") is tautologically
    tied to the maintenance hold, but no path resets it on its own once the
    operator clears the hold — ``attempt_auto_recovery`` only fires while the
    device is still ``offline`` and never runs once the device is back to
    ``available``. Without this clear the device renders as
    ``recovery_state="suppressed"`` ("Unhealthy") on the devices list even when
    every live signal is green.

    Caller must hold the device row lock and is responsible for the commit.
    """
    next_state = policy_state(device)
    record_maintenance_exited(next_state)
    write_state(device, next_state)


session_viability.configure_health_failure_handler(handle_health_failure)
