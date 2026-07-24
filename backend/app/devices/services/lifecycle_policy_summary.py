from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.timeutil import now_utc
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON
from app.devices.services.recovery_projection import SUPPRESSED_KINDS, recovery_availability
from app.devices.services.serialization_types import ReservationReadFacts
from app.devices.services.state import derive_operational_state
from app.lifecycle.services import remediation_log
from app.runs import service_reservation as run_reservation_service
from app.runs.models import TERMINAL_STATES

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import Device, DeviceOperationalState, DeviceReservation
    from app.devices.services.recovery_projection import RecoveryAvailability
    from app.lifecycle.services.remediation_log import LadderState
    from app.runs.models import TestRun


def freeze_reservation_context(
    run: TestRun | None,
    reservation_entry: DeviceReservation | None,
    *,
    device_id: uuid.UUID,
) -> ReservationReadFacts | None:
    """Copy the reservation scalars off the ORM run/entry while the read session is
    open, so the synchronous builders never re-touch the rows. Returns ``None`` when
    the device carries no active reservation."""
    if run is None:
        return None
    return ReservationReadFacts(
        run_id=run.id,
        run_name=run.name,
        run_state=run.state.value,
        run_terminal=run.state in TERMINAL_STATES,
        excluded=run_reservation_service.reservation_entry_is_excluded(reservation_entry),
        exclusion_kind=reservation_entry.exclusion_kind if reservation_entry else None,
        exclusion_reason=reservation_entry.exclusion_reason if reservation_entry else None,
        excluded_at=reservation_entry.excluded_at if reservation_entry else None,
        excluded_until=reservation_entry.excluded_until if reservation_entry else None,
        cooldown_count=reservation_entry.cooldown_count if reservation_entry else 0,
        blocks_allocation=run_reservation_service.reservation_gating_run_id(run, device_id) is not None,
    )


def derive_run_tracking_from_facts(reservation: ReservationReadFacts | None) -> dict[str, Any]:
    if reservation is None or reservation.run_terminal:
        return {
            "excluded_from_run": False,
            "excluded_run_id": None,
            "excluded_run_name": None,
            "excluded_at": None,
            "will_auto_rejoin_run": False,
        }

    excluded = reservation.excluded
    return {
        "excluded_from_run": excluded,
        "excluded_run_id": str(reservation.run_id) if excluded else None,
        "excluded_run_name": reservation.run_name if excluded else None,
        "excluded_at": (reservation.excluded_at.isoformat() if excluded and reservation.excluded_at else None),
        "will_auto_rejoin_run": excluded,
    }


def build_lifecycle_policy_from_facts(
    device: Device,
    *,
    ladder: LadderState,
    reservation: ReservationReadFacts | None,
    availability: RecoveryAvailability,
    operational_state: DeviceOperationalState,
    now: datetime,
) -> dict[str, Any]:
    policy = remediation_log.build_policy_view(ladder, device.lifecycle_policy_state)
    policy.update(derive_run_tracking_from_facts(reservation))
    backoff_until = ladder.backoff_active(now=now)
    if policy.get("deferred_stop"):
        recovery_state = "waiting_for_session_end"
    elif backoff_until is not None:
        recovery_state = "backoff"
    elif availability.kind in SUPPRESSED_KINDS:
        recovery_state = "suppressed"
    elif policy.get("excluded_from_run") or operational_state.value == "offline":
        recovery_state = "eligible"
    else:
        recovery_state = "idle"
    policy["recovery_state"] = recovery_state
    # Computed, never stored: keeps the API dict key (frontend panel + harness read it).
    policy["recovery_suppressed_reason"] = availability.reason if recovery_state == "suppressed" else None
    return policy


async def build_lifecycle_policy(
    db: AsyncSession,
    device: Device,
    reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
    *,
    ready: bool | None = None,
    operational_state: DeviceOperationalState | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or now_utc()
    ladder = await remediation_log.load_ladder(db, device.id)
    if reservation_context is None:
        reservation_context = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
    run, entry = reservation_context
    reservation = freeze_reservation_context(run, entry, device_id=device.id)
    availability = await recovery_availability(db, device, ready=ready, now=now)
    if operational_state is None:
        operational_state = await derive_operational_state(db, device, now=now)
    return build_lifecycle_policy_from_facts(
        device,
        ladder=ladder,
        reservation=reservation,
        availability=availability,
        operational_state=operational_state,
        now=now,
    )


def build_lifecycle_policy_summary(policy: dict[str, Any]) -> dict[str, Any]:
    current_state = policy.get("recovery_state")
    detail: str | None = None
    summary_state = DeviceLifecyclePolicySummaryState.idle
    label = "Idle"

    if policy.get("deferred_stop"):
        summary_state = DeviceLifecyclePolicySummaryState.deferred_stop
        label = "Stopping Soon"
        detail = policy.get("deferred_stop_reason") or "Waiting for the active client session to finish"
    elif current_state == "backoff":
        summary_state = DeviceLifecyclePolicySummaryState.backoff
        label = "Waiting to Retry"
        detail = policy.get("recovery_suppressed_reason") or policy.get("last_failure_reason")
    elif policy.get("excluded_from_run"):
        summary_state = DeviceLifecyclePolicySummaryState.excluded
        label = "Excluded from Run"
        run_name = policy.get("excluded_run_name") or "active run"
        detail = f"Excluded from {run_name}"
    elif current_state == "suppressed":
        summary_state = DeviceLifecyclePolicySummaryState.suppressed
        label = "Recovery Paused"
        suppression = policy.get("recovery_suppressed_reason")
        if suppression == MAINTENANCE_HOLD_SUPPRESSION_REASON:
            detail = policy.get("maintenance_reason") or suppression
        else:
            detail = suppression or policy.get("last_failure_reason")
    elif policy.get("last_failure_source") == "appium_reconciler" and policy.get("last_failure_reason"):
        summary_state = DeviceLifecyclePolicySummaryState.recoverable
        label = "Start Failed"
        detail = policy.get("last_failure_reason")
    elif current_state == "eligible":
        if policy.get("last_action") or policy.get("last_failure_reason"):
            summary_state = DeviceLifecyclePolicySummaryState.recoverable
            label = "Offline - Can Recover"
            detail = policy.get("last_failure_reason") or "Automatic recovery can run when the next check succeeds"
    return {
        "state": summary_state,
        "label": label,
        "detail": detail,
        "backoff_until": policy.get("backoff_until"),
        "maintenance_reason": policy.get("maintenance_reason"),
    }
