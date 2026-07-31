"""Single owner of the shared remediation-escalation ladder.

The ladder's memory is the append-only ``device_remediation_log`` table.
Every automated remediation -- recovery probe, node-health restart, appium
start retry -- shares one derived attempt count and backoff window. The
backoff saturates at ``general.lifecycle_recovery_backoff_max_sec``; attempts
continue indefinitely — there is no terminal rung.

Detection debounce (ip_ping duration windows, ``general.node_fail_window_sec``,
probe-unanswered counting, the link-repair attempt budget) stays with each
observer; this module owns only what happens AFTER a remediation fails.

This module never calls ``write_state`` (see
tests/lifecycle/test_lifecycle_write_state_allowlist.py); callers outside the
allowlist go through
``app.lifecycle.services.actions.escalate_device_remediation_failure``. It does
write the ``lifecycle_recovery_failed``/``lifecycle_recovery_backoff`` incident
pair, so that no escalation source can arm a backoff window silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.devices.models import DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.lifecycle.services import remediation_log
from app.lifecycle.services.incidents import LifecycleIncidentDetails

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.locking import LockedDevice
    from app.devices.models import Device
    from app.devices.services.decision_snapshot import ReservationDecisionSnapshot
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.lifecycle.services.remediation_log import LadderState


@dataclass(frozen=True)
class EscalationOutcome:
    backoff_until_iso: str
    attempts: int
    ladder: LadderState


@dataclass(frozen=True, slots=True)
class EscalationContext:
    """What the ladder needs to announce the failure it just recorded.

    Bundled rather than passed as three keywords: this module has no per-file
    PLR0913 exemption. ``reservation`` supplies the run context an operator needs
    to tell a fleet-wide fault from one run's device.
    """

    incidents: LifecycleIncidentService
    detail: str
    reservation: ReservationDecisionSnapshot | None = None


async def escalate_remediation_failure(
    db: AsyncSession,
    locked: LockedDevice,
    *,
    settings: SettingsReader,
    source: str,
    reason: str,
    context: EscalationContext,
    prior: LadderState | None = None,
) -> EscalationOutcome:
    """Record one failed automated remediation as an append-only attempt row, and
    announce it.

    Recording and announcing are deliberately one act (P2): an escalation that
    arms a backoff window without a durable event leaves a wedged device invisible
    to the device event feed and to SSE, which is exactly how the
    ``appium_reconciler`` start-failure path stayed silent.
    """
    entry, ladder = await remediation_log.append_attempt(
        db,
        locked,
        source=source,
        reason=reason,
        settings=settings,
        prior=prior,
    )
    assert entry.backoff_until is not None
    backoff_until_iso = entry.backoff_until.isoformat()
    await _announce(
        db,
        locked.device,
        context=context,
        source=source,
        reason=reason,
        backoff_until_iso=backoff_until_iso,
    )
    return EscalationOutcome(
        backoff_until_iso=backoff_until_iso,
        attempts=ladder.attempts,
        ladder=ladder,
    )


async def _announce(
    db: AsyncSession,
    device: Device,
    *,
    context: EscalationContext,
    source: str,
    reason: str,
    backoff_until_iso: str,
) -> None:
    reservation = context.reservation
    for event_type, detail in (
        (DeviceEventType.lifecycle_recovery_failed, context.detail),
        (
            DeviceEventType.lifecycle_recovery_backoff,
            "Automatic recovery is backing off before the next retry",
        ),
    ):
        await context.incidents.record_lifecycle_incident(
            db,
            device,
            event_type,
            LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.backoff,
                reason=reason,
                detail=detail,
                source=source,
                run_id=reservation.run_id if reservation is not None else None,
                run_name=reservation.run_name if reservation is not None else None,
                backoff_until=backoff_until_iso,
            ),
        )
