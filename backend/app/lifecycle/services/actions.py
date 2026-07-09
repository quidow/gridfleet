from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select

from app.appium_nodes.services import locking as appium_node_locking
from app.devices import locking as device_locking
from app.devices.models import DeviceEventType, DeviceOperationalState
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.event import build_device_crashed_payload, record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    NODE_PROCESS,
    IntentRegistration,
)
from app.devices.services.lifecycle_policy_state import (
    clear_backoff,
    clear_deferred_stop,
    set_action,
    write_state,
)
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.review import ReviewService
from app.lifecycle.services.escalation import EscalationOutcome, escalate_remediation_failure
from app.lifecycle.services.incidents import LifecycleIncidentDetails
from app.runs import service as run_reservation_service
from app.runs.models import TERMINAL_STATES
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.models import Device, DeviceReservation
    from app.devices.protocols import RunReservationWriter
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.runs.models import TestRun


class LifecyclePolicyActionsService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        reservation: RunReservationWriter,
        incidents: LifecycleIncidentService,
    ) -> None:
        self._publisher = publisher
        self._reservation = reservation
        self._incidents = incidents

    async def complete_auto_stop(
        self,
        db: AsyncSession,
        device: Device,
        next_state: dict[str, Any],
        *,
        reason: str,
        source: str,
        detail: str,
    ) -> tuple[TestRun | None, DeviceReservation | None]:
        device = await _lock_for_state_write(db, device)
        run, entry = await self.exclude_run_if_needed(db, device, reason=reason, source=source)
        await self.handle_node_crash(
            db,
            device,
            source=source,
            reason=reason,
        )
        next_state["stop_pending"] = False
        next_state["stop_pending_reason"] = None
        next_state["stop_pending_since"] = None
        await self.record_auto_stopped_incident(
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

    async def handle_node_crash(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> None:
        """Record a node crash and stop the underlying Appium node.

        Triggered by ``complete_auto_stop`` on ``connectivity_lost`` and
        ``health_check_fail`` in addition to genuine Appium crashes.

        Operational-state semantics:
        - Node running: writes desired_state='stopped' and lets the reconciler stop
          the agent process.
        - Node not running or absent (``else`` branch): forces ``offline`` directly
          using the already-held row lock; no re-acquisition needed.

        ``node_crash`` and ``device.crashed`` events fire only when the device is
        not already offline — a device that is already offline cannot crash again.
        The ``failure_event_type`` event always fires for observability.
        """
        device = await _lock_for_state_write(db, device)
        node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
        await record_event(
            db,
            device.id,
            failure_event_type(source),
            {"source": source, "reason": reason},
        )
        if device.operational_state != DeviceOperationalState.offline:
            await record_event(
                db,
                device.id,
                DeviceEventType.node_crash,
                {"error": reason, "source": source, "will_restart": True},
            )
            self._publisher.queue_for_session(
                db,
                "device.crashed",
                build_device_crashed_payload(
                    device_id=str(device.id),
                    device_name=device.name,
                    source=source,
                    reason=reason,
                    will_restart=True,
                ),
                severity="warning",
            )

        if source == "connectivity":
            # Connectivity loss is a fact, not a command: write device_checks_healthy=False
            # and let the reconciler synthesize the connectivity: defer-stop (session-safe,
            # priority 50) from it. Mirrors the no-node fact-write path below.
            device.device_checks_healthy = False
            device.device_checks_summary = reason
            await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            await db.commit()
            return

        if node is not None and node.observed_running:
            await IntentService(db).register_intents_and_reconcile(
                device_id=device.id,
                intents=_crash_intents(device),
                publisher=self._publisher,
            )
            await db.commit()
        else:
            if node is not None:
                await IntentService(db).register_intents_and_reconcile(
                    device_id=device.id,
                    intents=_crash_intents(device),
                    publisher=self._publisher,
                )
            else:
                # No node row — mark device_checks_healthy=False so the reconciler
                # derives offline (device_allows_allocation=False → ready=False).
                device.device_checks_healthy = False
                device.device_checks_summary = reason
                await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            await db.commit()

    async def exclude_run_if_needed(
        self, db: AsyncSession, device: Device, *, reason: str, source: str
    ) -> tuple[TestRun | None, DeviceReservation | None]:
        """Exclude the device from its active run reservation and emit the
        ``lifecycle_run_excluded`` incident.

        Called only from genuine exclusion-worthy paths: ``complete_auto_stop``
        (health failure) and the CI preparation-failure flow in
        ``run_service``. Connectivity loss is intentionally NOT a caller — a
        transient blip leaves the reservation entry intact (see D1).

        Does NOT escalate the device into maintenance. Auto-escalation to
        maintenance from health failures is intentionally absent. Maintenance is
        entered only by operator-driven UI actions and by run-failure escalation
        (CI preparation failure or cooldown threshold) when escalation-to-
        maintenance is enabled — those paths call
        ``maintenance_service.enter_maintenance`` explicitly. Callers here that
        need the device parked in maintenance must do the same.
        """
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        if run is None:
            return None, entry

        was_excluded = run_reservation_service.reservation_entry_is_excluded(entry)
        run = await self._reservation.exclude_device_from_run(db, device.id, reason=reason, commit=False)
        entry = run_reservation_service.get_reservation_entry_for_device(run, device.id) if run is not None else None
        if run is not None:
            # exclude_device_from_run wrote the indefinite exclusion on the reservation
            # row; the run: grid-routing intent derives from that row, so reconcile here
            # to drop it (the health-failure exclusion has no stored intent twin anymore).
            await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
        if run is not None and not was_excluded:
            await self._incidents.record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_run_excluded,
                LifecycleIncidentDetails(
                    summary_state=DeviceLifecyclePolicySummaryState.excluded,
                    reason=reason,
                    detail=f"Excluded from {run.name}",
                    source=source,
                    run_id=run.id,
                    run_name=run.name,
                ),
            )
        return run, entry

    async def restore_run_if_needed(
        self,
        db: AsyncSession,
        device: Device,
        run: TestRun | None,
        entry: DeviceReservation | None,
        *,
        reason: str,
        source: str,
    ) -> tuple[TestRun | None, DeviceReservation | None]:
        if (
            run is None
            or run.state in TERMINAL_STATES
            or not run_reservation_service.reservation_entry_is_excluded(entry)
        ):
            return run, entry

        run = await self._reservation.restore_device_to_run(db, device.id, commit=False)
        entry = run_reservation_service.get_reservation_entry_for_device(run, device.id) if run is not None else None
        if run is not None:
            # restore_device_to_run un-excluded the reservation row above; reconcile so
            # the run: grid-routing intent is re-derived.
            await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            await self._incidents.record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_run_restored,
                LifecycleIncidentDetails(
                    summary_state=DeviceLifecyclePolicySummaryState.idle,
                    reason=reason,
                    detail=f"Restored to {run.name}",
                    source=source,
                    run_id=run.id,
                    run_name=run.name,
                ),
            )
        return run, entry

    async def record_auto_stopped_incident(
        self,
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
        await self._incidents.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_auto_stopped,
            LifecycleIncidentDetails(
                summary_state=(
                    DeviceLifecyclePolicySummaryState.excluded
                    if run is not None
                    else DeviceLifecyclePolicySummaryState.recoverable
                ),
                reason=reason,
                detail=detail,
                source=source,
                run_id=run.id if run is not None else None,
                run_name=run.name if run is not None else None,
            ),
        )

    async def record_run_escalation_failure(
        self, db: AsyncSession, device: Device, *, reason: str, source: str, action: str
    ) -> None:
        """Persist run-escalation failure context onto ``lifecycle_policy_state``.

        Records the failure source/reason and the triggering ``action`` label
        (``ci_preparation_failed`` or ``cooldown_escalated``). Called only when
        the escalation enters maintenance; the maintenance hold itself drives the
        projected "Recovery Paused" badge, so no stored suppression is written.
        """
        device = await _lock_for_state_write(db, device)
        fresh = policy_state(device)
        fresh["last_failure_source"] = source
        fresh["last_failure_reason"] = reason
        clear_deferred_stop(fresh)
        clear_backoff(fresh)
        set_action(fresh, action)
        write_state(device, fresh)

    async def has_running_client_session(self, db: AsyncSession, device_id: uuid.UUID) -> bool:
        # Include ``pending``: a device in the grid allocate->confirm window is
        # already claimed by the router (the Appium create is in flight). This gates
        # auto-recovery (disruptive node restart) and auto-stop; restarting the node
        # mid-create yields "session not created" for the client (C7). Shared via
        # live_session_predicate so the gate cannot drift from the other live-session
        # sites.
        stmt = select(func.count()).select_from(Session).where(live_session_predicate(device_id))
        result = await db.execute(stmt)
        return bool(result.scalar_one())


async def _lock_for_state_write(db: AsyncSession, device: Device) -> Device:
    return await device_locking.lock_device(db, device.id, load_sessions=True)


def failure_event_type(source: str) -> DeviceEventType:
    return DeviceEventType.connectivity_lost if source == "connectivity" else DeviceEventType.health_check_fail


async def escalate_device_remediation_failure(
    db: AsyncSession,
    device: Device,
    *,
    settings: SettingsReader,
    source: str,
    reason: str,
) -> EscalationOutcome:
    """Shared-ladder escalation for callers outside the lifecycle write_state allowlist.

    Stamps the failure trail, records one failed remediation, and persists the
    mutated policy state. The caller holds the device row lock and owns the
    transaction commit.
    """
    fresh = policy_state(device)
    fresh["last_failure_source"] = source
    fresh["last_failure_reason"] = reason
    outcome = await escalate_remediation_failure(
        db,
        device,
        fresh,
        settings=settings,
        review=ReviewService(),
        source=source,
        reason=reason,
    )
    write_state(device, fresh)
    return outcome


def is_reconciler_failure_residue(state: dict[str, Any] | None) -> bool:
    """True when lifecycle policy state carries appium-reconciler start-failure residue."""
    if state is None:
        return False
    has_reconciler_failure = state.get("last_failure_source") == "appium_reconciler"
    has_orphaned_reason = bool(state.get("last_failure_reason") and not state.get("last_failure_source"))
    return has_reconciler_failure or has_orphaned_reason


def reset_reconciler_start_failure_state(device: Device) -> None:
    fresh = policy_state(device)
    if not is_reconciler_failure_residue(fresh):
        return
    fresh["last_failure_source"] = None
    fresh["last_failure_reason"] = None
    fresh["recovery_backoff_attempts"] = 0
    fresh["backoff_until"] = None
    write_state(device, fresh)


def _crash_intents(device: Device) -> list[IntentRegistration]:
    # Only the NODE_PROCESS stop intent is registered. The RECOVERY-axis
    # ``health_failure:recovery`` deny intent used to live here too, but it
    # had no expiry and gated the only code path that revoked it, deadlocking
    # any device that hit a transient probe failure. Recovery throttling is
    # now governed exclusively by the backoff window on
    # ``lifecycle_policy_state``; persistent failures eventually flip
    # ``Device.review_required`` and remove the device from the automated
    # recovery scope until an operator intervenes.
    return [
        IntentRegistration(
            source=f"health_failure:node:{device.id}",
            axis=NODE_PROCESS,
            payload={"action": "stop"},
        ),
    ]
