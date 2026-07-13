from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.appium_nodes.services import locking as appium_node_locking
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import DeviceEventType, DeviceOperationalState
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.event import build_device_crashed_payload, record_event
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent import IntentService
from app.devices.services.review import ReviewService
from app.devices.services.state import derive_operational_state
from app.lifecycle.services import remediation_log
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
        await self.record_auto_stopped_incident(
            db,
            device,
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
        operational_state = await derive_operational_state(db, device, now=now_utc())
        if operational_state != DeviceOperationalState.offline:
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
            # Connectivity loss is a fact, not a command: route through the guarded
            # device-health writer so the write takes the device row lock, draws a
            # fresh observation revision, and reconciles the connectivity defer-stop
            # (session-safe, priority 50). Mirrors the no-node fact-write path below.
            await DeviceHealthService(publisher=self._publisher).update_device_checks(
                db, device, healthy=False, summary=reason
            )
            await db.commit()
            return

        if node is not None and node.observed_running:
            await remediation_log.append_action(
                db,
                device.id,
                source=source,
                action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
                reason=reason,
            )
            await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            await db.commit()
        else:
            if node is not None:
                await remediation_log.append_action(
                    db,
                    device.id,
                    source=source,
                    action=remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
                    reason=reason,
                )
                await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            else:
                # No node row — route through the guarded device-health writer so
                # the reconciler derives offline (device_allows_allocation=False →
                # ready=False), taking the row lock and a fresh observation revision.
                await DeviceHealthService(publisher=self._publisher).update_device_checks(
                    db, device, healthy=False, summary=reason
                )
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
        *,
        run: TestRun | None,
        reason: str,
        source: str,
        detail: str,
    ) -> None:
        await remediation_log.append_action(
            db, device.id, source=source, action=remediation_log.ACTION_AUTO_STOPPED, reason=reason
        )
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
        await remediation_log.append_reset(db, device.id, source=source, action=action)
        await remediation_log.append_failure(db, device.id, source=source, reason=reason)

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
    """Shared-ladder escalation for callers outside the lifecycle write_state allowlist."""
    return await escalate_remediation_failure(
        db,
        device,
        settings=settings,
        review=ReviewService(),
        source=source,
        reason=reason,
    )


async def reset_reconciler_start_failure_if_needed(db: AsyncSession, device: Device) -> bool:
    """A successful node start supersedes only reconciler-sourced episodes."""
    ladder = await remediation_log.load_ladder(db, device.id)
    if not (ladder.armed or ladder.last_failure_reason) or ladder.last_failure_source != "appium_reconciler":
        return False
    await remediation_log.append_reset(db, device.id, source="appium_reconciler", action="start_succeeded")
    return True
