from __future__ import annotations

import asyncio
import logging
import random
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceOperationalState
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services import health as device_health
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.intent_types import (
    NODE_PROCESS,
    RECOVERY,
    IntentRegistration,
    failure_stop_sources,
)
from app.devices.services.lifecycle_policy_state import (
    CLIENT_SESSION_RUNNING_SUPPRESSION_REASON,
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_backoff,
    clear_deferred_stop,
    clear_self_heal_suppression,
    in_maintenance,
    loaded_node,
    now,
    record_backoff_suppressed,
    record_recovery_failed,
    record_recovery_recovered,
    record_recovery_started,
    set_action,
    set_deferred_stop,
    write_state,
)
from app.devices.services.lifecycle_policy_state import (
    state as policy_state,
)
from app.devices.services.readiness import is_ready_for_use_async
from app.lifecycle.services.escalation import backoff_active, escalate_remediation_failure
from app.lifecycle.services.incidents import LifecycleIncidentDetails
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
    from app.devices.models import DeviceReservation
    from app.devices.protocols import RemoteNodeManager, ReviewProtocol, SessionViabilityProbe
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.incidents import LifecycleIncidentService
    from app.runs.models import TestRun

logger = logging.getLogger(__name__)

# Self-heal suppression clear: residue must be older than this before a healthy
# connectivity tick is allowed to wipe it, so an in-flight failure sequence
# (verify-failure records suppression seconds before its auto-stop lands) is not
# clobbered by a transient healthy probe racing in between (regression S10).
_SELF_HEAL_MIN_AGE_INTERVAL_FACTOR = 2.0  # at least two connectivity check intervals
_SELF_HEAL_MIN_AGE_FLOOR_SEC = 120.0  # absolute floor (= factor x default 60s interval)


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
        device, current_state, run, entry, early = await self._evaluate_recovery_guards(
            db, device, source=source, reason=reason
        )
        if early is not None:
            return early

        backoff_until = backoff_active(current_state)
        if backoff_until is not None:
            record_backoff_suppressed(current_state, until_iso=backoff_until.isoformat())
            write_state(device, current_state)
            await db.commit()
            return False

        record_recovery_started(current_state)
        write_state(device, current_state)

        node = loaded_node(device)
        # An offline device has no usable running node, so a positive
        # ``observed_running`` reading here is necessarily stale: the appium
        # process is gone but the ``appium_reconciler`` has not yet cleared the
        # dead ``pid``/``active_connection_target`` (observed state is eventually
        # consistent, lagging up to one reconciler interval). Trusting it would
        # short-circuit the start path below — skipping the revoke of the
        # blocking ``health_failure:node`` stop intent — and strand the device in
        # backoff until the stale observation happens to clear. Treat it as not
        # running and (re)assert the node start.
        stale_offline_observation = (
            node is not None and node.observed_running and device.operational_state == DeviceOperationalState.offline
        )
        started_node = False
        if node is None or not node.observed_running or stale_offline_observation:
            started_node = True
            try:
                await self._register_recovery_start_intents(db, device, run=run, entry=entry, reason=reason)
            except NodeManagerError as exc:
                return await self._record_recovery_node_start_failure(
                    db, device, current_state, exc=exc, source=source, run=run
                )

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
            # Record a *skip* — not a suppression, not a failure: no auto-stop, no backoff, no
            # review_required, and crucially no ``suppressed`` badge. A
            # lock collision is benign and self-resolving — the flow that won the lock (the
            # exit-maintenance verification lease, or the next device_connectivity tick) does
            # the real recovery. (ex-N11 "Fix B": suppressing here raised a false "Recovery
            # Paused" alarm and tripped the harness's forbidden-event check in the S14 window.)
            return await self._actions.record_recovery_skipped(db, device)

        if result.get("status") != "passed":
            return await self._handle_recovery_probe_failure(db, device, current_state, result)

        return await self._finalize_recovery_success(db, device, current_state, source=source, reason=reason)

    async def _pre_reservation_suppression(
        self, db: AsyncSession, device: Device, current_state: dict[str, Any], *, source: str, reason: str
    ) -> bool | None:
        guards: list[tuple[bool, str]] = [
            (device.review_required, device.review_reason or "Device shelved — operator review required"),
            (
                not device.recovery_allowed,
                device.recovery_blocked_reason or "Recovery is blocked by orchestration intent",
            ),
        ]
        for tripped, suppression_reason in guards:
            if tripped:
                return await self._actions.record_recovery_suppressed(
                    db, device, source=source, reason=reason, suppression_reason=suppression_reason, run=None
                )
        return None

    async def _revoke_if_already_healthy(
        self, db: AsyncSession, device: Device, entry: DeviceReservation | None, *, reason: str
    ) -> bool:
        node = loaded_node(device)
        if (
            node is not None
            and node.observed_running
            and device.operational_state not in (DeviceOperationalState.offline, DeviceOperationalState.verifying)
            and not run_reservation_service.reservation_entry_is_excluded(entry)
        ):
            # Device is already healthy — recovery has nothing to start. Revoke
            # the stale stop intents anyway: a transient connectivity blip can
            # register a ``connectivity:{device_id}`` graceful-stop intent
            # without ever flipping the device offline (``_stop_disconnected_node``
            # only marks offline when the device is currently available). If the
            # blip clears and we land here, the intent persists at priority 50
            # forever, then a viability probe briefly holds a session, the
            # session-safety downgrade pins ``stop_pending=True``, and the
            # device flaps offline every probe cycle. Mirrors the revoke that
            # already fires in the start-node branch below.
            await IntentService(db).revoke_intents_and_reconcile(
                device_id=device.id,
                sources=failure_stop_sources(device.id),
                publisher=self._publisher,
            )
            return True
        return False

    async def _post_reservation_suppression(
        self,
        db: AsyncSession,
        device: Device,
        current_state: dict[str, Any],
        run: TestRun | None,
        entry: DeviceReservation | None,
        *,
        source: str,
        reason: str,
    ) -> bool | None:
        async def _suppress(suppression_reason: str) -> bool | None:
            return await self._actions.record_recovery_suppressed(
                db, device, source=source, reason=reason, suppression_reason=suppression_reason, run=run
            )

        if not await is_ready_for_use_async(db, device):
            return await _suppress("Device setup or verification is incomplete")
        if in_maintenance(device):
            return await _suppress(MAINTENANCE_HOLD_SUPPRESSION_REASON)
        if entry is not None and entry.excluded and entry.excluded_until is not None and entry.excluded_until > now():
            return await _suppress("Device is in active cooldown")
        if current_state.get("stop_pending"):
            return await _suppress("Waiting for active client session to finish")
        if await self._actions.has_running_client_session(db, device.id):
            return await _suppress(CLIENT_SESSION_RUNNING_SUPPRESSION_REASON)
        return None

    async def _evaluate_recovery_guards(
        self, db: AsyncSession, device: Device, *, source: str, reason: str
    ) -> tuple[Device, dict[str, Any], TestRun | None, DeviceReservation | None, bool | None]:
        device = await _reload_device(db, device)
        current_state = policy_state(device)
        pre = await self._pre_reservation_suppression(db, device, current_state, source=source, reason=reason)
        if pre is not None:
            return device, current_state, None, None, pre
        # D4: a stale ``stop_pending`` traps the device permanently when nothing
        # else clears it (no session row to fire ``handle_session_finished``).
        if current_state.get("stop_pending") and (
            device.operational_state == DeviceOperationalState.offline
            or not await self._actions.has_running_client_session(db, device.id)
        ):
            await self.clear_pending_auto_stop_on_recovery(
                db,
                device,
                source=current_state.get("last_failure_source") or source,
                reason="Cleared stale deferred stop before recovery",
                action="auto_stop_cleared",
                record_incident=False,
            )
            # Reload because the helper internally re-locks via _reload_device, returning a
            # different Device instance — our local reference is stale even though the writes
            # are already visible in the SQLAlchemy session.
            device = await _reload_device(db, device)
            current_state = policy_state(device)
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        if await self._revoke_if_already_healthy(db, device, entry, reason=reason):
            return device, current_state, run, entry, False
        post = await self._post_reservation_suppression(
            db, device, current_state, run, entry, source=source, reason=reason
        )
        return device, current_state, run, entry, post

    async def _register_recovery_start_intents(
        self, db: AsyncSession, device: Device, *, run: TestRun | None, entry: DeviceReservation | None, reason: str
    ) -> None:
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
        await IntentService(db).revoke_intents_and_reconcile(
            device_id=device.id,
            sources=failure_stop_sources(device.id),
            publisher=self._publisher,
        )

        # Recovery start intents are bounded by a DEADLINE (TTL) so the row self-expires:
        # a just-started node must not be torn down before the device finishes verifying
        # to ``available`` (recovery-suppressed-on-a-reachable-device regression). Mirrors
        # ``exit_maintenance``.
        startup_timeout = self._settings.get_int("appium.startup_timeout_sec")
        viability_timeout = self._settings.get_int("general.session_viability_timeout_sec")
        recovery_intent_expiry = now() + timedelta(seconds=startup_timeout + viability_timeout + 60)

        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[
                # The health-failure reservation exclusion is written directly on the
                # reservation row; run: routing derives from it. No stored twin needed.
                IntentRegistration(
                    source=f"auto_recovery:node:{device.id}",
                    axis=NODE_PROCESS,
                    payload={"action": "start"},
                    expires_at=recovery_intent_expiry,
                ),
                IntentRegistration(
                    source=f"auto_recovery:recovery:{device.id}",
                    axis=RECOVERY,
                    payload={"allowed": True, "reason": reason},
                    expires_at=recovery_intent_expiry,
                ),
            ],
            publisher=self._publisher,
        )
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
        current_state: dict[str, Any],
        *,
        exc: NodeManagerError,
        source: str,
        run: TestRun | None,
    ) -> bool:
        outcome = await escalate_remediation_failure(
            db,
            device,
            current_state,
            settings=self._settings,
            review=self._review,
            source=source,
            reason=str(exc),
        )
        record_recovery_failed(
            current_state,
            source=source,
            reason=str(exc),
            suppression_reason="Automatic restart failed",
        )
        write_state(device, current_state)
        await self._record_backoff_incident_pair(
            db,
            device,
            reason=str(exc),
            failure_detail=current_state["recovery_suppressed_reason"],
            source=source,
            run=run,
            backoff_until_iso=outcome.backoff_until_iso,
        )
        await db.commit()
        return False

    async def _handle_recovery_probe_failure(
        self, db: AsyncSession, device: Device, current_state: dict[str, Any], result: dict[str, Any]
    ) -> bool:
        failure_reason = result.get("error") or "Recovery viability probe failed"
        record_recovery_failed(
            current_state,
            source="session_viability",
            reason=failure_reason,
            suppression_reason="Recovery probe failed",
        )
        write_state(device, current_state)  # eager-write before potential intermediate commit in complete_auto_stop
        await self._actions.complete_auto_stop(
            db,
            device,
            current_state,
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
        fresh_state = policy_state(device)
        outcome = await escalate_remediation_failure(
            db,
            device,
            fresh_state,
            settings=self._settings,
            review=self._review,
            source="session_viability",
            reason=failure_reason,
        )
        record_recovery_failed(
            fresh_state,
            source="session_viability",
            reason=failure_reason,
            suppression_reason="Recovery probe failed",
        )
        write_state(device, fresh_state)
        await self._record_backoff_incident_pair(
            db,
            device,
            reason=failure_reason,
            failure_detail=fresh_state["recovery_suppressed_reason"],
            source="session_viability",
            run=run,
            backoff_until_iso=outcome.backoff_until_iso,
        )
        await db.commit()
        return False

    async def _finalize_recovery_success(
        self, db: AsyncSession, device: Device, current_state: dict[str, Any], *, source: str, reason: str
    ) -> bool:
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
            if device.operational_state != DeviceOperationalState.available:
                await IntentService(db).reconcile_now(device.id, publisher=self._publisher)
            await db.commit()

        await db.commit()

        # Re-lock for the trailing lifecycle write: the per-branch commits above
        # released the FOR UPDATE acquired earlier in this function.
        device = await device_locking.lock_device(db, device.id, load_sessions=True)
        fresh_state = policy_state(device)
        record_recovery_recovered(fresh_state)
        current_state = fresh_state
        write_state(device, current_state)
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
        current_state = policy_state(device)
        current_state["last_failure_source"] = source
        current_state["last_failure_reason"] = reason
        current_state["recovery_suppressed_reason"] = None
        # Persist this writer's intent into the session's identity map immediately
        # so the next intermediate commit lands these fields in the DB. Otherwise
        # downstream helpers that re-lock + refresh would lose this writer's intent
        # (see Task 9 / R4 — lock device row across lifecycle_policy_state RMW).
        write_state(device, current_state)

        if policy_state(device).get("maintenance_reason") is not None:
            await self._actions.record_recovery_suppressed(
                db,
                device,
                source=source,
                reason=reason,
                suppression_reason=MAINTENANCE_HOLD_SUPPRESSION_REASON,
                run=None,
            )
            return "suppressed"

        if await self._actions.has_running_client_session(db, device.id):
            set_deferred_stop(current_state, reason=reason)
            write_state(device, current_state)
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
            await db.commit()
            return "deferred"

        await self._actions.complete_auto_stop(
            db,
            device,
            current_state,
            reason=reason,
            source=source,
            detail="Manager stopped the device automatically after a lifecycle failure",
        )
        return "stopped"

    async def handle_session_finished(self, db: AsyncSession, device: Device) -> DeferredStopOutcome:
        device = await _reload_device(db, device)
        # Re-run intent reconciliation now that the session has ended. A previous
        # reconcile may have held a graceful-stop intent because the session was
        # running (see ``intent_reconciler.reconcile_device`` session-safety
        # invariant); with the session done, the held intent now applies and the
        # node converges to ``desired_state=stopped``. Independent of and runs
        # before the ``policy_state["stop_pending"]`` path below — that path
        # tracks lifecycle-policy-driven deferrals; this one covers
        # intent-driven deferrals registered by any caller.
        await reconcile_device(db, device.id, publisher=self._publisher)
        current_state = policy_state(device)

        # Clear any stale "A client session is still running" suppression recorded by
        # ``attempt_auto_recovery`` while a previous session was active. That branch
        # does not set ``stop_pending``, so nothing else clears the reason once the
        # session ends — the device renders as ``Unhealthy: A client session is still
        # running`` indefinitely (lifecycle_policy_summary maps the suppression to
        # ``state=suppressed`` → rendered as an error-tone state in the frontend).
        if current_state.get("recovery_suppressed_reason") == CLIENT_SESSION_RUNNING_SUPPRESSION_REASON:
            current_state["recovery_suppressed_reason"] = None
            set_action(current_state, "recovery_unsuppressed_after_session_end")
            write_state(device, current_state)
            await db.commit()

        if not current_state.get("stop_pending"):
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
            return DeferredStopOutcome.NO_PENDING_OR_RECOVERED

        reason = (
            current_state.get("stop_pending_reason")
            or current_state.get("last_failure_reason")
            or "Health-driven stop pending"
        )
        source = current_state.get("last_failure_source") or "device_checks"
        await self._actions.complete_auto_stop(
            db,
            device,
            current_state,
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
        current_state = policy_state(device)
        current_state["last_failure_source"] = "connectivity"
        current_state["last_failure_reason"] = reason
        clear_deferred_stop(current_state)
        # Persist intent before any await/commit (see handle_health_failure).
        write_state(device, current_state)

        # D1: connectivity loss is not exclusion-worthy. The device transitions
        # to offline through the connectivity loop's _stop_disconnected_node /
        # node_health paths; the reservation entry stays intact and the scheduler
        # treats offline devices as temporarily unavailable until recovery.
        await self._actions.record_auto_stopped_incident(
            db,
            device,
            current_state,
            run=None,
            reason=reason,
            source="connectivity",
            detail="Manager marked the device offline after connectivity loss",
        )

    async def clear_suppression_on_self_heal(self, db: AsyncSession, device: Device, *, reason: str) -> bool:
        """Clear recovery-suppression residue when a healthy device self-healed.

        Covers the gap that the operator-start (``clear_operator_start_suppression``)
        and maintenance-exit (``clear_maintenance_recovery_suppression``) paths do
        not: an agent restart → reconvergence leaves the node running and the
        device available without any recovery path firing, so a stale
        ``recovery_suppressed_reason`` (e.g. "Recovery probe failed") keeps the
        device deriving ``recovery_state="suppressed"`` forever.

        Caller (``device_connectivity`` healthy path) has already established the
        device is healthy and not offline. We additionally gate on
        ``device.recovery_allowed``: an active operator-stop deny intent (RECOVERY
        axis) flips that to False, which makes the suppression a legitimate,
        operator-owned hold — it must stay sticky (N13).

        Final gate: only clear residue older than ``min_age_seconds`` (>= 2x the
        connectivity check interval). A verify-failure records the suppression
        seconds before its auto-stop lands, and a concurrent healthy connectivity
        tick can race in between — clearing fresh residue would wipe an in-flight
        shelving sequence (regression S10). Returns True when residue was actually
        cleared so the incident fires exactly once; subsequent cycles no-op.
        """
        device = await _reload_device(db, device)
        if not device.recovery_allowed:
            return False
        check_interval = self._settings.get_float("general.device_check_interval_sec")
        # Residue must survive at least two connectivity ticks before we treat it
        # as stale, so an in-flight failure sequence (suppression → auto-stop) is
        # never wiped by a transient healthy probe landing between the two.
        min_age_seconds = max(_SELF_HEAL_MIN_AGE_FLOOR_SEC, _SELF_HEAL_MIN_AGE_INTERVAL_FACTOR * check_interval)
        cleared = clear_self_heal_suppression(device, min_age_seconds=min_age_seconds)
        if not cleared:
            return False
        await self._incidents.record_lifecycle_incident(
            db,
            device,
            DeviceEventType.lifecycle_recovered,
            LifecycleIncidentDetails(
                summary_state=DeviceLifecyclePolicySummaryState.idle,
                reason=reason,
                detail="Device self-healed; cleared stale recovery suppression",
                source="device_checks",
            ),
        )
        # No commit here: the connectivity loop is single-batch-commit and owns the
        # transaction boundary (wave-5 #6) — a mid-cycle commit would make partial
        # cycle state durable past a later rollback. Flush so the incident row is
        # visible to same-transaction reads.
        await db.flush()
        return True

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
        recovery-restore. Gated on ``recovery_allowed`` so an operator-stop hold stays
        sticky (mirrors ``clear_suppression_on_self_heal``). Cooldown exclusions
        (``excluded_until`` in the future) are intentional backoff and are left
        untouched. Returns True only when an exclusion was actually cleared.
        """
        device = await _reload_device(db, device)
        if not device.recovery_allowed:
            return False
        if device.operational_state != DeviceOperationalState.available:
            return False
        run, entry = await run_reservation_service.get_device_reservation_with_entry(db, device.id)
        if run is None or run.state in TERMINAL_STATES:
            return False
        if not run_reservation_service.reservation_entry_is_excluded(entry):
            return False
        if entry is not None and entry.excluded_until is not None and entry.excluded_until > now():
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


RECOVERY_PROBE_ATTEMPTS = 3
RECOVERY_PROBE_RETRY_DELAY_SEC = 10
RECOVERY_PROBE_JITTER_MAX_SEC = 2
RECOVERY_NODE_START_WAIT_TIMEOUT_SEC = 60
RECOVERY_NODE_START_WAIT_POLL_SEC = 0.5


class DeferredStopOutcome(StrEnum):
    """Outcome of ``complete_deferred_stop_if_session_ended`` / ``handle_session_finished``.

    NO_PENDING_OR_RECOVERED: either the device had no ``stop_pending`` intent, or it
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
