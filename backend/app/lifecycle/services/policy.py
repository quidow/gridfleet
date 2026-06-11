from __future__ import annotations

import logging
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tenacity import AsyncRetrying, RetryError, retry_if_result, stop_after_attempt, wait_fixed, wait_random

from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType, DeviceOperationalState
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services import health as device_health
from app.devices.services import lifecycle_policy_summary as lifecycle_policy_summary
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_reconciler import reconcile_device
from app.devices.services.intent_types import (
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    PRIORITY_HEALTH_FAILURE,
    RECOVERY,
    RESERVATION,
    IntentRegistration,
    NodeRunningPrecondition,
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
    parse_iso,
    record_backoff_suppressed,
    record_recovery_failed,
    record_recovery_recovered,
    record_recovery_started,
    set_action,
    set_deferred_stop,
    write_state,
)
from app.devices.services.lifecycle_policy_state import (
    set_backoff as _set_backoff_with_settings,
)
from app.devices.services.lifecycle_policy_state import (
    state as policy_state,
)
from app.devices.services.readiness import is_ready_for_use_async
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
    from app.devices.protocols import RemoteNodeManager, ReviewProtocol, SessionViabilityProbe
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.incidents import LifecycleIncidentService

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
        device = await _reload_device(db, device)
        current_state = policy_state(device)

        if device.review_required:
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason=device.review_reason or "Device shelved — operator review required",
                run=None,
            )

        if not device.recovery_allowed:
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason=device.recovery_blocked_reason or "Recovery is blocked by orchestration intent",
                run=None,
            )

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
                sources=[
                    f"connectivity:{device.id}",
                    f"health_failure:node:{device.id}",
                    f"health_failure:recovery:{device.id}",
                ],
                reason=reason,
                publisher=self._publisher,
            )
            return False

        if not await is_ready_for_use_async(db, device):
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason="Device setup or verification is incomplete",
                run=run,
            )
        if in_maintenance(device):
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason=MAINTENANCE_HOLD_SUPPRESSION_REASON,
                run=run,
            )
        if entry is not None and entry.excluded and entry.excluded_until is not None and entry.excluded_until > now():
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason="Device is in active cooldown",
                run=run,
            )
        if current_state.get("stop_pending"):
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason="Waiting for active client session to finish",
                run=run,
            )
        if await self._actions.has_running_client_session(db, device.id):
            return await self._actions.record_recovery_suppressed(
                db,
                device,
                current_state,
                source=source,
                reason=reason,
                suppression_reason=CLIENT_SESSION_RUNNING_SUPPRESSION_REASON,
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
                    sources=[
                        f"connectivity:{device.id}",
                        f"health_failure:node:{device.id}",
                        f"health_failure:recovery:{device.id}",
                    ],
                    reason=reason,
                    publisher=self._publisher,
                )

                def _node_not_running_precondition() -> NodeRunningPrecondition:
                    return {
                        "kind": "node_running",
                        "device_id": str(device.id),
                        "expected": False,
                    }

                # The recovery start intents normally carry the node_running
                # precondition so they auto-retire once the node is observed
                # running. But when the observation is stale (stale_offline_observation),
                # that precondition keys off the same unreliable
                # ``observed_running`` and the precondition sweep would reap the
                # intents within one reconciler tick — dropping desired_state back
                # to ``stopped`` before the agent can start the node. Bound them by
                # a deadline instead (the sweep cannot reap a TTL'd intent; it
                # self-expires), mirroring ``exit_maintenance``.
                if stale_offline_observation:
                    startup_timeout = self._settings.get_int("appium.startup_timeout_sec")
                    viability_timeout = self._settings.get_int("general.session_viability_timeout_sec")
                    recovery_intent_precondition = None
                    recovery_intent_expiry = now() + timedelta(seconds=startup_timeout + viability_timeout + 60)
                else:
                    recovery_intent_precondition = _node_not_running_precondition()
                    recovery_intent_expiry = None

                await IntentService(db).register_intents_and_reconcile(
                    device_id=device.id,
                    intents=[
                        *(
                            [
                                IntentRegistration(
                                    source=f"health_failure:reservation:{device.id}",
                                    axis=RESERVATION,
                                    run_id=run.id,
                                    payload={
                                        "excluded": True,
                                        "priority": PRIORITY_HEALTH_FAILURE,
                                        "exclusion_reason": entry.exclusion_reason,
                                    },
                                )
                            ]
                            if run is not None
                            and entry is not None
                            and run_reservation_service.reservation_entry_is_excluded(entry)
                            else []
                        ),
                        IntentRegistration(
                            source=f"auto_recovery:node:{device.id}",
                            axis=NODE_PROCESS,
                            payload={
                                "action": "start",
                                "priority": PRIORITY_AUTO_RECOVERY,
                            },
                            precondition=recovery_intent_precondition,
                            expires_at=recovery_intent_expiry,
                        ),
                        IntentRegistration(
                            source=f"auto_recovery:recovery:{device.id}",
                            axis=RECOVERY,
                            payload={"allowed": True, "priority": PRIORITY_AUTO_RECOVERY, "reason": reason},
                            precondition=recovery_intent_precondition,
                            expires_at=recovery_intent_expiry,
                        ),
                    ],
                    reason=reason,
                    publisher=self._publisher,
                )
                await db.commit()
                await db.refresh(device.appium_node)
            except NodeManagerError as exc:
                backoff_until_iso = _set_backoff(current_state, settings=self._settings)
                record_recovery_failed(
                    current_state,
                    source=source,
                    reason=str(exc),
                    suppression_reason="Automatic restart failed",
                )
                write_state(device, current_state)
                await self._incidents.record_lifecycle_incident(
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
                await self._incidents.record_lifecycle_incident(
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
            # review_required, and crucially no ``suppressed``/``needs_attention`` badge. A
            # lock collision is benign and self-resolving — the flow that won the lock (the
            # exit-maintenance verification lease, or the next device_connectivity tick) does
            # the real recovery. (ex-N11 "Fix B": suppressing here raised a false "Recovery
            # Paused" alarm and tripped the harness's forbidden-event check in the S14 window.)
            return await self._actions.record_recovery_skipped(db, device)

        if result.get("status") != "passed":
            failure_reason = result.get("error") or "Recovery viability probe failed"
            backoff_until_iso = _set_backoff(current_state, settings=self._settings)
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
            await self._incidents.record_lifecycle_incident(
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
            await self._incidents.record_lifecycle_incident(
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
            # Promote to review_required once consecutive failures cross the
            # operator-configurable threshold. After this point the device drops
            # out of automated recovery scope and only a sanctioned operator
            # action (exit maintenance, restore from run, re-verify, restart
            # node) clears the flag.
            review_threshold = self._settings.get_int("general.lifecycle_recovery_review_threshold")
            attempts = int(fresh_state.get("recovery_backoff_attempts") or 0)
            if attempts >= review_threshold:
                await self._review.mark_review_required(
                    db,
                    device,
                    reason=failure_reason,
                    source="session_viability",
                )
                await db.commit()
            else:
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
                await IntentService(db).mark_dirty_and_reconcile(
                    device.id, reason=f"Recovery ({source}): {reason}", publisher=self._publisher
                )
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
            summary_state=DeviceLifecyclePolicySummaryState.idle,
            reason=reason,
            detail="Device recovered successfully",
            source=source,
            run_id=run.id if run is not None else None,
            run_name=run.name if run is not None else None,
        )
        await db.commit()
        return True

    async def _run_recovery_probe(self, db: AsyncSession, device: Device) -> dict[str, Any]:
        last_result: dict[str, Any] = {}
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max(1, RECOVERY_PROBE_ATTEMPTS)),
                wait=wait_fixed(RECOVERY_PROBE_RETRY_DELAY_SEC) + wait_random(0, RECOVERY_PROBE_JITTER_MAX_SEC),
                retry=retry_if_result(lambda value: value.get("status") != "passed"),
            ):
                with attempt:
                    reloaded = await _reload_device(db, device)
                    try:
                        last_result = await self._viability.run_session_viability_probe(
                            db,
                            reloaded,
                            checked_by=SessionViabilityCheckedBy.recovery,
                        )
                    except SessionViabilityProbeInProgressError:
                        # Another viability probe (typically an active verification job) already
                        # holds this device's probe lock. That is a concurrency collision, not a
                        # device-health signal — counting it as a failed attempt would feed
                        # recovery backoff/review and could shelve a healthy device. Skip and let
                        # the lifecycle loop retry on its next cycle once the lock frees; do NOT
                        # retry here (the lock will not free within the short retry window).
                        logger.info(
                            "Recovery viability probe for device %s skipped — another viability probe is in progress",
                            device.id,
                        )
                        return {"status": "skipped"}
                    except SessionViabilityProbeNotPermittedError:
                        # The device left a probeable state (e.g. it was allocated and went
                        # ``busy``, or an operator parked it in ``maintenance``) between the
                        # recovery decision and the probe. That is a gating rejection, not a
                        # health signal — counting it as a failed attempt would feed
                        # backoff/review and could shelve a healthy device. Skip and let the
                        # lifecycle loop retry on its next cycle (mirrors the in-progress
                        # collision above); do NOT retry here (the state will not flip back
                        # within the short retry window).
                        logger.info(
                            "Recovery viability probe for device %s skipped — device state no longer permits a probe",
                            device.id,
                        )
                        return {"status": "skipped"}
                    except Exception as exc:  # noqa: BLE001 — a probe error must not crash the recovery job
                        # Fold an unexpected probe error (transient "already in
                        # progress" race, concurrent state change, etc.) into a failed
                        # result so the retry loop re-probes and, if it persists, the
                        # caller's failure terminal applies backoff and schedules a later
                        # retry — rather than propagating out of attempt_auto_recovery and
                        # stranding the device until the verification lease's expires_at
                        # safety net fires.
                        logger.warning(
                            "Recovery viability probe for device %s raised %s; treating as a failed attempt",
                            device.id,
                            type(exc).__name__,
                            exc_info=True,
                        )
                        last_result = {"status": "failed", "error": str(exc)}
                if attempt.retry_state.outcome is not None and not attempt.retry_state.outcome.failed:
                    attempt.retry_state.set_result(last_result)
        except RetryError:
            pass  # Exhausted attempts — return last_result below
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
                current_state,
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
                summary_state=DeviceLifecyclePolicySummaryState.deferred_stop,
                reason=reason,
                detail="Waiting for the active client session to finish",
                source=source,
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
            return DeferredStopOutcome.NO_PENDING

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
            return DeferredStopOutcome.CLEARED_RECOVERED

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
            summary_state=DeviceLifecyclePolicySummaryState.idle,
            reason=reason,
            detail="Device self-healed; cleared stale recovery suppression",
            source="device_checks",
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
                summary_state=DeviceLifecyclePolicySummaryState.idle,
                reason=reason,
                detail=detail,
                source=source,
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


def _set_backoff(state: dict[str, Any], *, settings: SettingsReader) -> str:
    base_seconds = settings.get_int("general.lifecycle_recovery_backoff_base_sec")
    max_seconds = max(base_seconds, settings.get_int("general.lifecycle_recovery_backoff_max_sec"))
    return _set_backoff_with_settings(state, base_seconds=base_seconds, max_seconds=max_seconds)


async def _reload_device(db: AsyncSession, device: Device) -> Device:
    return await device_locking.lock_device(db, device.id, load_sessions=True)
