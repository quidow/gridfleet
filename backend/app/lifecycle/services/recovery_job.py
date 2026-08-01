"""Durable-job worker that runs auto-recovery effects outside fold transactions.

The worker splits recovery into phases that each own and close their own DB
session before any remote HTTP call or retry sleep, so no DB transaction is
held across the Appium probe or the node-start wait:

* ``_ensure_prepared`` — lock the device, load one snapshot. A matching
  ``recovery_generation`` means the fold already prepared the node; a
  different generation means the job is stale. No generation means a legacy
  maintenance-created job, so ``prepare_auto_recovery_locked`` runs with the
  job id as the generation. A device that is already healthy (available +
  running node + no active exclusion) with a matching generation is finalized
  as ``already_healthy`` without a probe.
* ``_wait_for_node_running`` — poll the node row with a fresh session per
  read, closing it before each sleep.
* ``_run_probe`` — retry loop; every attempt owns and closes its own session
  before the retry sleep. ``run_session_viability_probe`` commits the probe
  claim before the remote ``probe_session_direct`` call, so no DB transaction
  is open during the HTTP probe.
* ``_finalize_device`` — lock the device once, load one snapshot, and call
  ``finalize_auto_recovery_locked`` (which compares the generation before any
  write).
* ``_finalize_job`` — mark the job row terminal in its own transaction (also
  the terminal path for a lock failure, on a session that failure never
  touched).
"""

from __future__ import annotations

import asyncio
import copy
import random
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from app.appium_nodes.models import AppiumNode
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.intent import IntentService
from app.devices.services.lifecycle_policy_state import (
    clear_recovery_generation,
    recovery_generation,
)
from app.devices.services.state import evaluate_operational_state
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from app.jobs.models import Job
from app.lifecycle.services import remediation_log
from app.sessions.service_viability import (
    SessionViabilityProbeInProgressError,
    SessionViabilityProbeNotPermittedError,
)
from app.sessions.viability_types import SessionViabilityCheckedBy

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.protocols import SettingsReader
    from app.devices.protocols import SessionViabilityProbe
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.policy import LifecyclePolicyService

logger = get_logger(__name__)

RECOVERY_PROBE_ATTEMPTS = 3
RECOVERY_PROBE_RETRY_DELAY_SEC = 10
RECOVERY_PROBE_JITTER_MAX_SEC = 2
RECOVERY_NODE_START_WAIT_TIMEOUT_SEC = 60
RECOVERY_NODE_START_WAIT_POLL_SEC = 0.5

# Sentinel probe results for the ``_ensure_prepared`` terminal outcomes.
_ALREADY_HEALTHY: dict[str, Any] = {"status": "already_healthy"}
_STALE: dict[str, Any] = {"status": "stale"}


class _DeviceLockFailed(Exception):  # noqa: N818 - prescribed private sentinel
    """The device-lock statement failed, so this transaction may be aborted.

    Raised to escape the phase's ``begin()`` block — which rolls back — rather
    than writing the job row on a session whose next COMMIT would raise. Private
    so it can never be confused with a domain error a caller might handle.
    """


class RecoveryJobService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: EventPublisher,
        settings: SettingsReader,
        lifecycle_policy: LifecyclePolicyService,
        viability: SessionViabilityProbe,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._settings = settings
        self._lifecycle_policy = lifecycle_policy
        self._viability = viability

    @staticmethod
    def _stage_job_row(
        row: Job,
        *,
        status: str,
        note: str | None = None,
        error: str | None = None,
    ) -> None:
        """Stage the job row's terminal state; the caller's boundary commits it."""
        row.status = status
        snapshot = copy.deepcopy(row.snapshot)
        snapshot["status"] = status
        if note is not None:
            snapshot["note"] = note
        if error is not None:
            snapshot["error"] = error
        snapshot["finished_at"] = now_utc().isoformat()
        row.snapshot = snapshot
        row.completed_at = now_utc()

    async def _ensure_prepared(  # noqa: PLR0911 - one return per terminal/proceed branch
        self,
        parsed_job_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        source: str,
        reason: str,
    ) -> tuple[dict[str, Any], uuid.UUID | None]:
        """Lock the device, load one snapshot, and decide the terminal/proceed path.

        One explicit transaction. ``db.get(Job, ...)`` is an unlocked read, and
        every ``_stage_job_row`` call below sits textually under the
        ``lock_device_handle`` call, so this phase never dirties a job tuple
        above the device lock — see the Job/Device order note in
        ``app/devices/services/remediation_job.py`` and the contract in
        ``tests/contracts/test_remediation_job_lock_order.py``.
        """
        try:
            async with self._session_factory.begin() as db:
                row = await db.get(Job, parsed_job_id)
                try:
                    locked = await device_locking.lock_device_handle(db, device_id)
                except NoResultFound:
                    # A clean "no such row" from the lock query: the transaction
                    # is healthy, so the job row can be staged right here.
                    logger.info("device_recovery: device %s no longer exists; marking job complete", device_id)
                    if row is not None:
                        self._stage_job_row(row, status=JOB_STATUS_COMPLETED, note="Device no longer exists")
                    return ({"status": "device_missing"}, None)
                except Exception as exc:
                    # A statement error, which aborts the transaction. Writing the
                    # job row here would be staged into a transaction whose COMMIT
                    # raises, so escape the boundary instead.
                    raise _DeviceLockFailed from exc
                snapshot = await load_device_decision_snapshot(db, locked, now=now_utc())
                current = recovery_generation(locked.device)
                if current is not None and current != parsed_job_id:
                    if row is not None:
                        self._stage_job_row(row, status=JOB_STATUS_COMPLETED, note="stale generation")
                    return (_STALE, None)
                if current == parsed_job_id:
                    node_id = locked.device.appium_node.id if locked.device.appium_node is not None else None
                    if (
                        evaluate_operational_state(snapshot.state_facts) == DeviceOperationalState.available
                        and snapshot.node_observed_running
                        and not snapshot.decision_facts.in_maintenance
                        and (snapshot.reservation is None or not snapshot.reservation.excluded)
                    ):
                        if snapshot.ladder.episode_active:
                            reset = await remediation_log.append_reset(
                                db, locked, source=source, action="already_healthy", reason=reason
                            )
                            updated = replace(
                                snapshot,
                                ladder=remediation_log.advance_ladder(snapshot.ladder, reset),
                                decision_facts=replace(snapshot.decision_facts, remediation_directive=None),
                            )
                            await IntentService(db).reconcile_locked(
                                locked, publisher=self._publisher, snapshot=updated
                            )
                        clear_recovery_generation(locked.device, expected=parsed_job_id)
                        if row is not None:
                            self._stage_job_row(row, status=JOB_STATUS_COMPLETED, note="already_healthy")
                        return (_ALREADY_HEALTHY, None)
                    return ({}, node_id)
                prepared = await self._lifecycle_policy.prepare_auto_recovery_locked(
                    db,
                    locked,
                    snapshot,
                    generation=parsed_job_id,
                    source=source,
                    reason=reason,
                    enqueue_job=False,
                )
                if not prepared:
                    # The blocked path's prepare work and the job row now land in
                    # one transaction; they used to be two commits. Strictly
                    # stronger — a crash between them could previously leave the
                    # device prepared with the job still pending.
                    if row is not None:
                        self._stage_job_row(row, status=JOB_STATUS_COMPLETED, note="recovery blocked")
                    return ({"status": "blocked"}, None)
                return ({}, locked.device.appium_node.id if locked.device.appium_node is not None else None)
        except _DeviceLockFailed:
            # ``logger.exception`` here still carries the original traceback: the
            # sentinel was raised ``from exc``.
            logger.exception("device_recovery: failed to lock device %s", device_id)
            await self._finalize_job(
                parsed_job_id, status=JOB_STATUS_FAILED, error=f"Device {device_id} could not be locked"
            )
            return ({"status": "lock_failed"}, None)

    async def _wait_for_node_running(self, node_id: uuid.UUID) -> bool:
        deadline = time.monotonic() + RECOVERY_NODE_START_WAIT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            async with self._session_factory() as db:
                row = (
                    await db.execute(
                        select(AppiumNode.pid, AppiumNode.active_connection_target).where(AppiumNode.id == node_id)
                    )
                ).one_or_none()
            if row is not None and row.pid is not None and row.active_connection_target is not None:
                return True
            await asyncio.sleep(RECOVERY_NODE_START_WAIT_POLL_SEC)
        return False

    async def _run_probe(self, device_id: uuid.UUID) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for attempt in range(max(1, RECOVERY_PROBE_ATTEMPTS)):
            # Preflight: confirm the device still exists in a short read session
            # that is CLOSED before the viability command runs. The viability
            # probe owns its own fresh sessions (prepare/confirm/finalize/escalate);
            # no session or ORM ``Device`` crosses into it — only the device id.
            async with self._session_factory() as db:
                exists = (await db.execute(select(Device.id).where(Device.id == device_id))).one_or_none()
            if exists is None:
                return {"status": "skipped", "note": "device no longer exists"}
            try:
                last = await self._viability.run_session_viability_probe(
                    device_id,
                    checked_by=SessionViabilityCheckedBy.recovery,
                )
            except SessionViabilityProbeInProgressError, SessionViabilityProbeNotPermittedError, ValueError:
                # A collision, a gate rejection, or a readiness recheck lapse are all
                # preconditions that lapsed — not a device verdict — so none of them
                # should commission recovery failure/backoff work. The two named
                # types already narrow this; adding the base ``ValueError`` catches
                # the readiness recheck's own raise (``_prepare_probe``) without
                # widening the catch to unrelated exceptions.
                return {"status": "skipped"}
            except Exception as exc:  # noqa: BLE001 - failed effect is finalized durably
                last = {"status": "failed", "error": str(exc)}
            if last.get("status") == "passed":
                return last
            if attempt < RECOVERY_PROBE_ATTEMPTS - 1:
                await asyncio.sleep(RECOVERY_PROBE_RETRY_DELAY_SEC + random.uniform(0, RECOVERY_PROBE_JITTER_MAX_SEC))
        return last

    async def _finalize_device(
        self,
        parsed_job_id: uuid.UUID,
        device_id: uuid.UUID,
        *,
        result: dict[str, Any],
        source: str,
        reason: str,
    ) -> str:
        async with self._session_factory.begin() as db:
            locked = await device_locking.lock_device_handle(db, device_id)
            snapshot = await load_device_decision_snapshot(db, locked, now=now_utc())
            return await self._lifecycle_policy.finalize_auto_recovery_locked(
                db,
                locked,
                snapshot,
                generation=parsed_job_id,
                result=result,
                source=source,
                reason=reason,
            )

    async def _finalize_job(
        self,
        parsed_job_id: uuid.UUID,
        *,
        status: str = JOB_STATUS_COMPLETED,
        note: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self._session_factory.begin() as db:
            row = await db.get(Job, parsed_job_id)
            if row is not None:
                self._stage_job_row(row, status=status, note=note, error=error)

    async def _clear_generation_and_fail(self, parsed_job_id: uuid.UUID, device_id: uuid.UUID) -> None:
        async with self._session_factory.begin() as db:
            try:
                locked = await device_locking.lock_device_handle(db, device_id)
            except NoResultFound:
                locked = None
            if locked is not None:
                clear_recovery_generation(locked.device, expected=parsed_job_id)
        await self._finalize_job(
            parsed_job_id, status=JOB_STATUS_FAILED, error="device_recovery job crashed unexpectedly"
        )

    async def run_device_recovery_job(self, job_id: str, payload: dict[str, Any]) -> None:
        parsed_job_id = uuid.UUID(job_id)
        source = payload.get("source", "exit_maintenance")
        reason = payload.get("reason", "Operator exited maintenance")
        try:
            device_id = uuid.UUID(str(payload["device_id"]))
        except Exception:
            logger.exception("device_recovery: malformed payload for job %s", job_id)
            await self._finalize_job(
                parsed_job_id, status=JOB_STATUS_FAILED, error="device_recovery job crashed unexpectedly"
            )
            return

        try:
            probe_result, node_id = await self._ensure_prepared(parsed_job_id, device_id, source=source, reason=reason)
            terminal = probe_result.get("status")
            if terminal in ("device_missing", "stale", "already_healthy", "blocked", "lock_failed"):
                return
            if node_id is not None:
                await self._wait_for_node_running(node_id)
            result = await self._run_probe(device_id)
            await self._finalize_device(parsed_job_id, device_id, result=result, source=source, reason=reason)
            await self._finalize_job(parsed_job_id)
        except Exception:
            logger.exception("device_recovery: job %s for device %s crashed", job_id, payload.get("device_id"))
            await self._clear_generation_and_fail(parsed_job_id, device_id)
