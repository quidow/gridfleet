from __future__ import annotations

import asyncio
import logging
import random
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceOperationalState, ExclusionKind
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services import health as device_health
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.lifecycle_policy_state import loaded_node, now
from app.devices.services.lifecycle_policy_state import state as policy_state
from app.devices.services.recovery_projection import recovery_availability
from app.devices.services.state import derive_operational_state
from app.lifecycle.services import remediation_log
from app.lifecycle.services.escalation import escalate_remediation_failure
from app.lifecycle.services.incidents import LifecycleIncidentDetails
from app.lifecycle.services.operator_node import operator_stop_active
from app.runs import service_reservation as run_reservation_service
from app.runs.models import TERMINAL_STATES
from app.sessions.service_viability import (
    SessionViabilityProbeInProgressError,
    SessionViabilityProbeNotPermittedError,
)
from app.sessions.viability_types import SessionViabilityCheckedBy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.devices.locking import LockedDevice
    from app.devices.models import DeviceReservation
    from app.devices.protocols import RemoteNodeManager, ReviewProtocol, SessionViabilityProbe
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.runs.models import TestRun

logger = logging.getLogger(__name__)


class LifecyclePolicyService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        actions: LifecyclePolicyActionsService,
        incidents: LifecycleIncidentService,
        viability: SessionViabilityProbe,
        node_manager: RemoteNodeManager,
        review: ReviewProtocol,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._actions = actions
        self._incidents = incidents
        self._viability = viability
        self._node_manager = node_manager
        self._review = review

    async def attempt_auto_recovery(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> bool:
        device, run, _entry, early = await self._evaluate_recovery_guards(db, device, source=source, reason=reason)
        if early is not None:
            return early

        await remediation_log.append_action(
            db, device.id, source=source, action=remediation_log.ACTION_RECOVERY_STARTED
        )

        node = loaded_node(device)
        operational_state = await derive_operational_state(db, device, now=now_utc())
        # An offline device has no usable running node, so a positive
        # ``observed_running`` reading here is necessarily stale: the appium
        # process is gone but the ``appium_reconciler`` has not yet cleared the
        # dead ``pid``/``active_connection_target`` (observed state is eventually
        # consistent, lagging up to one reconciler interval). Trusting it would
        # short-circuit the start path below — skipping the reconcile that applies
        # the START directive over the stop directive — and strand the device in
        # backoff until the stale observation happens to clear. Treat it as not
        # running and (re)assert the node start.
        stale_offline_observation = (
            node is not None and node.observed_running and operational_state == DeviceOperationalState.offline
        )
        started_node = False
        if node is None or not node.observed_running or stale_offline_observation:
            started_node = True
            try:
                await self._ensure_recovery_node_row(db, device)
            except NodeManagerError as exc:
                return await self._record_recovery_node_start_failure(db, device, exc=exc, source=source, run=run)

        # Wait for the reconciler to observe the node running before probing.
        # The reconciler runs asynchronously; probing immediately after
        # registering the start intent races the agent start-up.
        if started_node and device.appium_node is not None:
            observed = await self._node_manager.wait_for_node_running(
                db,
                device.appium_node.id,
                timeout_sec=RECOVERY_NODE_START_WAIT_TIMEOUT_SEC,
                poll_interval_sec=RECOVERY_NODE_START_WAIT_POLL_SEC,
            )
            if observed is None:
                logger.warning(
                    "Recovery: node %s for device %s did not become observed_running within timeout; "
                    "proceeding with probe anyway",
                    device.appium_node.id,
                    device.id,
                )

        result = await self._run_recovery_probe(db, device)

        if result.get("status") == "skipped":
            # The probe could not run because another viability probe holds the device's
            # lock, or the device left a probeable state mid-attempt (see _run_recovery_probe).
            # A skip is not a failure: no auto-stop, no backoff, no review_required, and no
            # state write at all — the badge is projected from live facts, so a benign,
            # self-resolving lock collision leaves nothing to clear. The flow that won the
            # lock (the exit-maintenance verification lease, or the next device_connectivity
            # tick) does the real recovery.
            logger.info("auto-recovery skipped for device %s — probe could not run (transient)", device.id)
            return False

        if result.get("status") != "passed":
            return await self._handle_recovery_probe_failure(db, device, result)

        return await self._finalize_recovery_success(db, device, source=source, reason=reason)

    async def _reset_if_already_healthy(
        self,
        db: AsyncSession,
        device: Device,
        entry: DeviceReservation | None,
        *,
        source: str,
        reason: str,
    ) -> bool:
        node = loaded_node(device)
        operational_state = await derive_operational_state(db, device, now=now_utc())
        if (
            node is not None
            and node.observed_running
            and operational_state not in (DeviceOperationalState.offline, DeviceOperationalState.verifying)
            and not run_reservation_service.reservation_entry_is_excluded(entry)
        ):
            # Recovery has nothing to start. Supersede any leftover episode so a
            # derived stop directive cannot re-apply after this healthy observation.
            ladder = await remediation_log.load_ladder(db, device.id)
            if ladder.episode_active:
                await remediation_log.append_reset(
                    db, device.id, source=source, action="already_healthy", reason=reason
                )
                await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            return True
        return False

    async def _evaluate_recovery_guards(
        self, db: AsyncSession, device: Device, *, source: str, reason: str
    ) -> tuple[Device, TestRun | None, DeviceReservation | None, bool | None]:
        device = await _reload_device(db, device)
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        if await self._reset_if_already_healthy(db, device, entry, source=source, reason=reason):
            return device, run, entry, False
        availability = await recovery_availability(db, device)
        if not availability.allowed:
            logger.info(
                "auto-recovery blocked for device %s: %s (%s)", device.id, availability.reason, availability.kind
            )
            return device, run, entry, False
        return device, run, entry, None

    async def _ensure_recovery_node_row(self, db: AsyncSession, device: Device) -> None:
        if device.host_id is None:
            raise NodeManagerError(f"Device {device.id} has no host assigned")
        if device.appium_node is None:
            # Port allocation is only needed when creating the node row.
            # For the stale-offline case the node row already exists, so
            # skip the allocation query entirely.
            desired_port = (await candidate_ports(db, host_id=device.host_id, settings=self._settings))[0]
            new_node = AppiumNode(
                device_id=device.id,
                port=desired_port,
            )
            db.add(new_node)
            await db.flush()
            device.appium_node = new_node
        await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
        await db.commit()
        await db.refresh(device.appium_node)

    async def _record_backoff_incident_pair(
        self,
        db: AsyncSession,
        device: Device,
        *,
        reason: str,
        failure_detail: str,
        source: str,
        run: TestRun | None,
        backoff_until_iso: str | None,
    ) -> None:
        run_id = run.id if run is not None else None
        run_name = run.name if run is not None else None
        await self._incidents.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovery_failed,
            LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.backoff,
                reason=reason,
                detail=failure_detail,
                source=source,
                run_id=run_id,
                run_name=run_name,
                backoff_until=backoff_until_iso,
            ),
        )
        await self._incidents.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovery_backoff,
            LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.backoff,
                reason=reason,
                detail="Automatic recovery is backing off before the next retry",
                source=source,
                run_id=run_id,
                run_name=run_name,
                backoff_until=backoff_until_iso,
            ),
        )

    async def _record_recovery_node_start_failure(
        self,
        db: AsyncSession,
        device: Device,
        *,
        exc: NodeManagerError,
        source: str,
        run: TestRun | None,
    ) -> bool:
        outcome = await escalate_remediation_failure(
            db,
            device,
            settings=self._settings,
            review=self._review,
            source=source,
            reason=str(exc),
        )
        await self._record_backoff_incident_pair(
            db,
            device,
            reason=str(exc),
            failure_detail="Automatic restart failed",
            source=source,
            run=run,
            backoff_until_iso=outcome.backoff_until_iso,
        )
        await db.commit()
        return False

    async def _handle_recovery_probe_failure(self, db: AsyncSession, device: Device, result: dict[str, Any]) -> bool:
        failure_reason = result.get("error") or "Recovery viability probe failed"
        await self._actions.complete_auto_stop(
            db,
            device,
            reason=failure_reason,
            source="session_viability",
            detail="Manager stopped the device after a failed recovery viability probe",
        )

        # Re-lock and rebuild state from fresh DB row: complete_auto_stop releases
        # the row lock via intermediate commits in handle_node_crash. Escalation
        # (attempt count, backoff, review promotion) runs on the fresh row so it
        # can never clobber a concurrent writer on the same device.
        device = await device_locking.lock_device(db, device.id, load_sessions=True)
        run, _entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        outcome = await escalate_remediation_failure(
            db,
            device,
            settings=self._settings,
            review=self._review,
            source="session_viability",
            reason=failure_reason,
        )
        await self._record_backoff_incident_pair(
            db,
            device,
            reason=failure_reason,
            failure_detail="Recovery probe failed",
            source="session_viability",
            run=run,
            backoff_until_iso=outcome.backoff_until_iso,
        )
        await db.commit()
        return False

    async def _finalize_recovery_success(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> bool:
        device = await device_locking.lock_device(db, device.id, load_sessions=True)
        # Re-resolve the reservation under lock as well:
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)

        if run is not None and run.state not in TERMINAL_STATES:
            if run_reservation_service.reservation_entry_is_excluded(entry):
                run, entry = await self._actions.restore_run_if_needed(
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
                await db.commit()
            else:
                await db.commit()
        else:
            await record_event(
                db,
                device.id,
                DeviceEventType.node_restart,
                {"recovered_from": source, "reason": reason},
            )
            await db.commit()

        await db.commit()

        device = await device_locking.lock_device(db, device.id, load_sessions=True)
        await remediation_log.append_reset(db, device.id, source=source, action="auto_recovered", reason=reason)
        await self._incidents.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovered,
            LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.idle,
                reason=reason,
                detail="Device recovered successfully",
                source=source,
                run_id=run.id if run is not None else None,
                run_name=run.name if run is not None else None,
            ),
        )
        await db.commit()
        return True

    async def _run_recovery_probe(self, db: AsyncSession, device: Device) -> dict[str, Any]:
        last_result: dict[str, Any] = {}
        attempts = max(1, RECOVERY_PROBE_ATTEMPTS)
        for attempt in range(attempts):
            reloaded = await _reload_device(db, device)
            try:
                last_result = await self._viability.run_session_viability_probe(
                    db,
                    reloaded,
                    checked_by=SessionViabilityCheckedBy.recovery,
                )
            except SessionViabilityProbeInProgressError:
                logger.info(
                    "Recovery viability probe for device %s skipped — another viability probe is in progress",
                    device.id,
                )
                return {"status": "skipped"}
            except SessionViabilityProbeNotPermittedError:
                logger.info(
                    "Recovery viability probe for device %s skipped — device state no longer permits a probe",
                    device.id,
                )
                return {"status": "skipped"}
            except Exception as exc:  # noqa: BLE001 — a probe error must not crash the recovery job
                logger.warning(
                    "Recovery viability probe for device %s raised %s; treating as a failed attempt",
                    device.id,
                    type(exc).__name__,
                    exc_info=True,
                )
                last_result = {"status": "failed", "error": str(exc)}
            if last_result.get("status") == "passed":
                return last_result
            if attempt < attempts - 1:
                # ponytail: stdlib retry; fixed delay + jitter, no third-party retry lib
                await asyncio.sleep(RECOVERY_PROBE_RETRY_DELAY_SEC + random.uniform(0, RECOVERY_PROBE_JITTER_MAX_SEC))
        return last_result

    async def handle_health_failure(self, db: AsyncSession, device: Device, *, source: str, reason: str) -> str:
        device = await _reload_device(db, device)
        outcome = await self._prepare_health_failure(db, device, source=source, reason=reason)
        if outcome == "deferred":
            await db.commit()
        elif outcome == "stopped":
            await self._actions.complete_auto_stop(
                db,
                device,
                reason=reason,
                source=source,
                detail="Manager stopped the device automatically after a lifecycle failure",
            )
        return outcome

    async def handle_health_failure_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        *,
        source: str,
        reason: str,
    ) -> str:
        locked.assert_active(db)
        outcome = await self._prepare_health_failure(
            db,
            locked.device,
            source=source,
            reason=reason,
        )
        if outcome == "stopped":
            await self._actions.complete_auto_stop_locked(
                db,
                locked,
                source=source,
                reason=reason,
                detail="Manager stopped the device automatically after a lifecycle failure",
            )
        return outcome

    async def _prepare_health_failure(
        self,
        db: AsyncSession,
        device: Device,
        *,
        source: str,
        reason: str,
    ) -> str:
        await remediation_log.append_failure(db, device.id, source=source, reason=reason)

        if policy_state(device).get("maintenance_reason") is not None:
            logger.info("health failure on maintenance-held device %s suppressed: %s", device.id, reason)
            return "suppressed"

        if await self._actions.has_running_client_session(db, device.id):
            await remediation_log.append_action(
                db,
                device.id,
                source=source,
                action=remediation_log.ACTION_AUTO_STOP_DEFERRED,
                reason=reason,
            )
            await self._incidents.record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_deferred_stop,
                LifecycleIncidentDetails(
                    summary_state=DeviceLifecyclePolicySummaryState.deferred_stop,
                    reason=reason,
                    detail="Waiting for the active client session to finish",
                    source=source,
                ),
            )
            return "deferred"
        return "stopped"

    async def handle_session_finished(self, db: AsyncSession, device: Device) -> DeferredStopOutcome:
        device = await _reload_device(db, device)
        # Re-run intent reconciliation now that the session has ended. A previous
        # reconcile may have held a graceful-stop directive because the session was
        # running (see ``intent_reconciler.reconcile_device`` session-safety
        # invariant); with the session done, the held intent now applies and the
        # node converges to ``desired_state=stopped``.
        await reconcile_device(db, device.id, publisher=self._publisher)
        ladder = await remediation_log.load_ladder(db, device.id)
        if not ladder.deferred_stop_pending:
            return DeferredStopOutcome.NO_PENDING_OR_RECOVERED

        # Authoritative check under the device row lock. Callers may have
        # pre-validated outside the lock for early-exit, but a fresh client
        # session can start between that check and the lock; only the locked
        # check is safe.
        if await self._actions.has_running_client_session(db, device.id):
            return DeferredStopOutcome.RUNNING_SESSION_EXISTS

        node = loaded_node(device)
        node_running = node is not None and node.observed_running

        if device_health.merged_liveness(device) is True and node_running:
            # Defense in depth: ``clear_pending_auto_stop_on_recovery`` should
            # already have cleared the intent when health recovered. If anything
            # slipped the device into a healthy state without going through that
            # path, the row-derived projection is treated as the canonical health
            # source. A subsequent failed probe will re-enter
            # ``handle_health_failure``.
            await self.clear_pending_auto_stop_on_recovery(
                db,
                device,
                source=ladder.last_failure_source or "session",
                reason="Session finished while device was healthy",
            )
            # Mirror the AUTO_STOPPED branch (which commits via ``complete_auto_stop``):
            # commit the cleared intent here so callers do not need to know about the
            # internal helper contract. Without this commit, request-scoped sessions
            # (FastAPI ``get_db``) close before the cleared state is persisted, and
            # the dashboard keeps rendering stale ``deferred_stop``.
            await db.commit()
            return DeferredStopOutcome.NO_PENDING_OR_RECOVERED

        reason = ladder.deferred_stop_reason or ladder.last_failure_reason or "Health-driven stop pending"
        source = ladder.last_failure_source or "device_checks"
        await self._actions.complete_auto_stop(
            db,
            device,
            reason=reason,
            source=source,
            detail="Manager completed a previously deferred automatic stop",
        )
        return DeferredStopOutcome.AUTO_STOPPED

    async def complete_deferred_stop_if_session_ended(self, db: AsyncSession, device: Device) -> DeferredStopOutcome:
        """Idempotent session-end helper. Authoritative state checks live in
        ``handle_session_finished``, which re-reads under the device row lock —
        so callers do not need to (and must not) pre-validate state.
        """
        return await self.handle_session_finished(db, device)

    async def note_connectivity_loss(self, db: AsyncSession, device: Device, *, reason: str) -> None:
        device = await _reload_device(db, device)
        await remediation_log.append_failure(db, device.id, source="connectivity", reason=reason)

        # D1: connectivity loss is not exclusion-worthy. The device transitions
        # to offline through the connectivity loop's _stop_disconnected_node /
        # node_health paths; the reservation entry stays intact and the scheduler
        # treats offline devices as temporarily unavailable until recovery.
        await self._actions.record_auto_stopped_incident(
            db,
            device,
            run=None,
            reason=reason,
            source="connectivity",
            detail="Manager marked the device offline after connectivity loss",
        )

    async def clear_escalation_residue_on_self_heal(self, db: AsyncSession, device: Device, *, reason: str) -> bool:
        """Reset the shared escalation ladder when a healthy device self-healed.

        Covers the gap that the operator-start reset
        path does not: an agent restart → reconvergence leaves the node running and
        the device available without any recovery path firing, so a stale backoff
        window / attempt counter keeps the node's effective-state ``blocked`` forever.

        Caller (``device_connectivity`` healthy path) has already established the
        device is healthy and not offline. We additionally gate on
        ``operator_stop_active``: an active operator-stop deny intent (operator_recovery_deny kind)
        makes the hold legitimate and operator-owned — it must stay sticky (N13).

        A healthy tick racing an in-flight failure sequence may supersede that
        episode immediately; the append-only trail remains intact and the next
        failure starts a fresh episode (WS-15.1 plan-time narrowing 4). Returns
        True when a reset was appended so the incident fires exactly once;
        subsequent cycles no-op.
        """
        locked = await device_locking.lock_device_handle(db, device.id)
        operator_stopped = await operator_stop_active(db, locked.device.id)
        return await self._clear_escalation_residue_locked(
            db,
            locked.device,
            operator_stopped=operator_stopped,
            reason=reason,
        )

    async def _clear_escalation_residue_locked(
        self,
        db: AsyncSession,
        device: Device,
        *,
        operator_stopped: bool,
        reason: str,
    ) -> bool:
        if operator_stopped:
            return False
        ladder = await remediation_log.load_ladder(db, device.id)
        if not ladder.episode_active:
            return False
        await remediation_log.append_reset(db, device.id, source="device_checks", action="self_healed", reason=reason)
        await self._incidents.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovered,
            LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.idle,
                reason=reason,
                detail="Device self-healed; reset stale escalation residue",
                source="device_checks",
            ),
        )
        # No commit here: the connectivity fold owns the per-device transaction
        # boundary. A nested commit would make partial device work durable past a
        # later failure. Flush so the incident row is visible to same-transaction reads.
        await db.flush()
        return True

    async def reconcile_self_heal_locked(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        *,
        operational_state: DeviceOperationalState,
        residue_reason: str,
        run_reason: str,
    ) -> tuple[bool, bool]:
        """Clear healthy-path lifecycle residue through an already-held device lock."""
        locked.assert_active(db)
        device = locked.device
        operator_stopped = await operator_stop_active(db, device.id)
        cleared = await self._clear_escalation_residue_locked(
            db,
            device,
            operator_stopped=operator_stopped,
            reason=residue_reason,
        )
        restored = await self._restore_run_after_self_heal_locked(
            db,
            device,
            operational_state=operational_state,
            operator_stopped=operator_stopped,
            reason=run_reason,
        )
        return cleared, restored

    async def restore_run_after_self_heal(self, db: AsyncSession, device: Device, *, reason: str) -> bool:
        """Clear a stale permanent (health-failure) run exclusion once the device is
        provably available again.

        Counterpart to ``restore_run_if_needed``'s viability-pass path. A device can
        return to ``available`` via a route that never runs auto-recovery — operator
        node restart (``operator_node`` revokes the node-stop intent but not the
        reservation one), exit-maintenance, or an independent reconverge. That leaves
        the no-TTL ``health_failure:reservation`` intent active, so the reconciler keeps
        the run exclusion forever and the device sits ``available``/green yet excluded.

        An ``available`` device is already deemed allocatable (``merged_liveness`` is
        not False), so rejoining its run is safe and consistent with the existing
        recovery-restore. Gated on ``operator_stop_active`` so an operator-stop hold
        stays sticky (mirrors ``clear_escalation_residue_on_self_heal``). Cooldown
        exclusions (``excluded_until`` in the future) are intentional backoff and are
        left untouched. Returns True only when an exclusion was actually cleared.
        """
        locked = await device_locking.lock_device_handle(db, device.id)
        operator_stopped = await operator_stop_active(db, locked.device.id)
        operational_state = await derive_operational_state(db, locked.device, now=now_utc())
        return await self._restore_run_after_self_heal_locked(
            db,
            locked.device,
            operational_state=operational_state,
            operator_stopped=operator_stopped,
            reason=reason,
        )

    async def _restore_run_after_self_heal_locked(
        self,
        db: AsyncSession,
        device: Device,
        *,
        operational_state: DeviceOperationalState,
        operator_stopped: bool,
        reason: str,
    ) -> bool:
        if operator_stopped:
            return False
        if operational_state != DeviceOperationalState.available:
            return False
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        if run is None or run.state in TERMINAL_STATES:
            return False
        if not run_reservation_service.reservation_entry_is_excluded(entry):
            return False
        if (
            entry is not None
            and entry.exclusion_kind == ExclusionKind.cooldown
            and entry.excluded_until is not None
            and entry.excluded_until > now()
        ):
            return False  # active cooldown — intentional backoff, leave it
        await self._actions.restore_run_if_needed(db, device, run, entry, reason=reason, source="self_heal")
        return True

    async def clear_pending_auto_stop_on_recovery(
        self,
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
        ladder = await remediation_log.load_ladder(db, device.id)
        if not ladder.deferred_stop_pending:
            return False

        pending_since = ladder.deferred_stop_since
        pending_reason = ladder.deferred_stop_reason
        await remediation_log.append_action(
            db,
            device.id,
            source=source,
            action=action or remediation_log.ACTION_AUTO_STOP_CLEARED,
            reason=reason,
        )

        if record_incident:
            detail = (
                f"Recovery cleared deferred stop queued at {pending_since}"
                if pending_since
                else "Recovery cleared deferred stop"
            )
            if pending_reason:
                detail = f"{detail} (was: {pending_reason})"

            await self._incidents.record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovered,
                LifecycleIncidentDetails(
                    summary_state=DeviceLifecyclePolicySummaryState.idle,
                    reason=reason,
                    detail=detail,
                    source=source,
                ),
            )
        return True

    async def record_control_action(
        self,
        db: AsyncSession,
        device: Device,
        *,
        action: str,
        failure_source: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        device = await _reload_device(db, device)
        if failure_source is not None:
            await remediation_log.append_failure(
                db,
                device.id,
                source=failure_source,
                reason=failure_reason or "Control action failure",
            )
        await remediation_log.append_action(db, device.id, source=failure_source or "lifecycle", action=action)


RECOVERY_PROBE_ATTEMPTS = 3
RECOVERY_PROBE_RETRY_DELAY_SEC = 10
RECOVERY_PROBE_JITTER_MAX_SEC = 2
RECOVERY_NODE_START_WAIT_TIMEOUT_SEC = 60
RECOVERY_NODE_START_WAIT_POLL_SEC = 0.5


class DeferredStopOutcome(StrEnum):
    """Outcome of ``complete_deferred_stop_if_session_ended`` / ``handle_session_finished``.

    NO_PENDING_OR_RECOVERED: either the device had no ``deferred_stop`` intent, or it
        became healthy before the session ended and the intent was cleared without an
        auto-stop. In both cases the caller restores availability via the normal
        session-end-on-healthy-device path.
    RUNNING_SESSION_EXISTS: another client session is still running, so the helper
        bailed without touching state.
    AUTO_STOPPED: the deferred stop was completed; the device is offline (and excluded
        from any active run).
    """

    NO_PENDING_OR_RECOVERED = "no_pending_or_recovered"
    RUNNING_SESSION_EXISTS = "running_session_exists"
    AUTO_STOPPED = "auto_stopped"


async def _reload_device(db: AsyncSession, device: Device) -> Device:
    return await device_locking.lock_device(db, device.id, load_sessions=True)
