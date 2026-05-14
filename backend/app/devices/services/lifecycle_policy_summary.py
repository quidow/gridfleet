from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.devices.models import Device, DeviceOperationalState
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.lifecycle_policy_state import now, parse_iso, state
from app.runs import service_reservation as run_reservation_service
from app.runs.models import TERMINAL_STATES

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.models import DeviceReservation
    from app.runs.models import TestRun


def derive_run_tracking(
    reservation: TestRun | None,
    reservation_entry: DeviceReservation | None,
) -> dict[str, Any]:
    if reservation is None or reservation_entry is None or reservation.state in TERMINAL_STATES:
        return {
            "excluded_from_run": False,
            "excluded_run_id": None,
            "excluded_run_name": None,
            "excluded_at": None,
            "will_auto_rejoin_run": False,
        }

    excluded = run_reservation_service.reservation_entry_is_excluded(reservation_entry)
    return {
        "excluded_from_run": excluded,
        "excluded_run_id": str(reservation.id) if excluded else None,
        "excluded_run_name": reservation.name if excluded else None,
        "excluded_at": (
            reservation_entry.excluded_at.isoformat() if excluded and reservation_entry.excluded_at else None
        ),
        "will_auto_rejoin_run": excluded,
    }


async def build_lifecycle_policy(
    db: AsyncSession,
    device: Device,
    reservation_context: tuple[Any | None, DeviceReservation | None] | None = None,
) -> dict[str, Any]:
    policy = state(device)
    if reservation_context is None:
        reservation_context = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
    run, entry = reservation_context
    policy.update(derive_run_tracking(run, entry))

    backoff_until = parse_iso(policy.get("backoff_until"))
    if policy.get("stop_pending"):
        recovery_state = "waiting_for_session_end"
    elif backoff_until is not None and backoff_until > now():
        recovery_state = "backoff"
    elif policy.get("recovery_suppressed_reason"):
        recovery_state = "suppressed"
    elif policy.get("excluded_from_run") or device.operational_state == DeviceOperationalState.offline:
        recovery_state = "eligible" if device.auto_manage else "manual"
    else:
        recovery_state = "idle"

    policy["recovery_state"] = recovery_state
    return policy


def build_lifecycle_policy_summary(policy: dict[str, Any]) -> dict[str, Any]:
    current_state = policy.get("recovery_state")
    detail: str | None = None
    summary_state = DeviceLifecyclePolicySummaryState.idle
    label = "Idle"

    if policy.get("stop_pending"):
        summary_state = DeviceLifecyclePolicySummaryState.deferred_stop
        label = "Deferred Stop"
        detail = policy.get("stop_pending_reason") or "Waiting for the active client session to finish"
    elif current_state == "backoff":
        summary_state = DeviceLifecyclePolicySummaryState.backoff
        label = "Backing Off"
        detail = policy.get("recovery_suppressed_reason") or policy.get("last_failure_reason")
    elif policy.get("excluded_from_run"):
        summary_state = DeviceLifecyclePolicySummaryState.excluded
        label = "Excluded"
        run_name = policy.get("excluded_run_name") or "active run"
        detail = f"Excluded from {run_name}"
    elif current_state == "suppressed":
        summary_state = DeviceLifecyclePolicySummaryState.suppressed
        label = "Suppressed"
        detail = policy.get("recovery_suppressed_reason") or policy.get("last_failure_reason")
    elif policy.get("last_failure_source") == "appium_reconciler" and policy.get("last_failure_reason"):
        summary_state = DeviceLifecyclePolicySummaryState.recoverable
        label = "Node Start Failed"
        detail = policy.get("last_failure_reason")
    elif current_state == "eligible":
        if policy.get("last_action") or policy.get("last_failure_reason"):
            summary_state = DeviceLifecyclePolicySummaryState.recoverable
            label = "Recovery Eligible"
            detail = policy.get("last_failure_reason") or "Automatic recovery can run when the next check succeeds"
    elif current_state == "manual":
        if policy.get("last_action") or policy.get("last_failure_reason"):
            summary_state = DeviceLifecyclePolicySummaryState.manual
            label = "Manual Recovery"
            detail = policy.get("recovery_suppressed_reason") or "Automatic recovery is disabled"

    return {
        "state": summary_state,
        "label": label,
        "detail": detail,
        "backoff_until": policy.get("backoff_until"),
    }
