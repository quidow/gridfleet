"""Durable worker for repeat-safe device-health remediation actions.

The worker runs in three phases with explicit boundaries:

* ``_prepare`` — one ``session_factory.begin()``. Locks the ``Job`` root, then
  the ``Device`` row, reserves (or reuses) the repair-attempt number in
  ``Job.snapshot``, gathers the dispatch facts under the same device proof, and
  copies every scalar the remote call needs into a frozen ``RemediationEffect``.
  Nothing that declines to dispatch leaves the transaction: the matching ``Job``
  is completed in place.
* ``_dispatch`` — no session, no transaction, no lock. The agent call runs only
  on copied values.
* ``_finalize`` — a fresh ``begin()``. Re-locks ``Job`` then ``Device`` and
  applies the result only if the claim generation, the job status, the failure
  episode, and the unhealthy fact all still match.

``Job.id`` is the repeat-safe operation id: a crashed run is replayed under the
same id, and the reserved repair-attempt number is reused so a replay does not
burn a second slot of the attempt budget. The *generation* that decides which
worker may finish the job is ``Job.attempts`` as returned by the claim
statement, threaded in as ``claim_attempt``. It must never be re-derived by
reading ``Job.attempts`` inside ``_prepare``: a stale reset plus a reclaim by a
second worker landing in the claim-to-prepare window would give both workers the
same value, and both finalizations would then match.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

from app.agent_comm.operations import pack_device_lifecycle_action
from app.appium_nodes.services import resource_service as appium_node_resource_service
from app.core import metrics_recorders as metrics
from app.core.errors import AgentCallError
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.models.event import DeviceEventType
from app.devices.services import link_repair
from app.devices.services.event import record_event
from app.devices.services.lifecycle_policy_state import in_maintenance
from app.jobs import JOB_KIND_DEVICE_HEALTH_REMEDIATION, JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from app.jobs.models import Job
from app.jobs.statuses import JOB_STATUS_RUNNING
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session
from app.sessions.service import device_has_running_session

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.devices.protocols import DeviceHealthProtocol

logger = get_logger(__name__)

#: Snapshot key holding the repair-attempt number this ``Job.id`` reserved once.
SNAPSHOT_ATTEMPT_KEY = "remediation_attempt"
DETAIL_MAX_LEN = 200


@dataclass(frozen=True, slots=True)
class RemediationEffect:
    """Immutable dispatch values copied out from under the ``Job``/``Device`` locks."""

    job_id: uuid.UUID
    claim_attempt: int
    device_id: uuid.UUID
    failure_episode_id: uuid.UUID
    action: str
    repair_attempt: int
    host_ip: str
    host_agent_port: int
    connection_target: str
    pack_id: str
    platform_id: str
    ip_address: str | None
    extra_args: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class RemediationResult:
    success: bool
    detail: str


async def _host_has_live_sessions(db: AsyncSession, device: Device) -> bool:
    row = await db.execute(
        select(Session.id)
        .join(Device, Session.device_id == Device.id)
        .where(Device.host_id == device.host_id, live_session_predicate())
        .limit(1)
    )
    return row.first() is not None


def _complete(job: Job, *, note: str | None, error: str | None) -> None:
    """Terminate ``job`` in the caller's transaction. Never commits."""
    status = JOB_STATUS_FAILED if error is not None else JOB_STATUS_COMPLETED
    job.status = status
    snapshot = copy.deepcopy(job.snapshot)
    snapshot["status"] = status
    if note is not None:
        snapshot["note"] = note
    if error is not None:
        snapshot["error"] = error
    snapshot["finished_at"] = now_utc().isoformat()
    job.snapshot = snapshot
    job.completed_at = now_utc()


async def _lock_claimed_job(db: AsyncSession, job_id: uuid.UUID, claim_attempt: int) -> Job | None:
    """Lock the job root and return it only while it is still *this* claim's to run."""
    job = (await db.execute(select(Job).where(Job.id == job_id).with_for_update())).scalar_one_or_none()
    if job is None or job.kind != JOB_KIND_DEVICE_HEALTH_REMEDIATION:
        return None
    if job.status != JOB_STATUS_RUNNING or job.attempts != claim_attempt:
        return None
    return job


async def _reserve_repair_attempt(db: AsyncSession, job: Job, device: Device) -> int | None:
    """Reuse this job's reserved repair-attempt number, or draw one from the budget."""
    reserved = job.snapshot.get(SNAPSHOT_ATTEMPT_KEY)
    if isinstance(reserved, int):
        return reserved
    attempt = await link_repair.next_repair_attempt(db, device.identity_value)
    if attempt is None:
        return None
    snapshot = copy.deepcopy(job.snapshot)
    snapshot[SNAPSHOT_ATTEMPT_KEY] = attempt
    job.snapshot = snapshot
    return attempt


class RemediationJobService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        circuit_breaker: CircuitBreakerProtocol,
        health: DeviceHealthProtocol,
        pool: AgentHttpPool | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._circuit_breaker = circuit_breaker
        self._health = health
        self._pool = pool

    async def _prepare(
        self,
        job_id: uuid.UUID,
        payload: dict[str, Any],
        claim_attempt: int,
    ) -> RemediationEffect | None:
        device_id = uuid.UUID(str(payload["device_id"]))
        failure_episode_id = uuid.UUID(str(payload["failure_episode_id"]))
        action = str(payload["action_id"])

        async with self._session_factory.begin() as db:
            # Lock order here is Job then Device -- the opposite of the health
            # fold's Device-then-jobs-insert (connectivity.py's
            # _escalate_health_failure -> remediation.enqueue_device_health_remediation).
            # The inversion does not deadlock only because SELECT ... FOR UPDATE
            # leaves a lock-only xmax on the job row: Postgres's dirty-snapshot
            # check does not treat that as an xwait, so the fold's
            # INSERT ... ON CONFLICT DO NOTHING never blocks on it. The moment a
            # job.* write lands on this locked row before the device lock below
            # succeeds, the row carries a real update xmax and the fold's insert
            # does wait on it -- while this transaction is waiting on the device
            # lock the fold already holds. That is a confirmed deadlock, not a
            # theoretical one. So every job.* write in this method (including a
            # hoisted _complete(job, ...)) must stay strictly below the
            # lock_device_handle() call. The one exception is the
            # NoResultFound branch right below: it runs only when that call
            # itself failed to acquire the device lock, so there is no device
            # lock for a fold transaction to be holding.
            job = await _lock_claimed_job(db, job_id, claim_attempt)
            if job is None:
                return None
            try:
                locked = await device_locking.lock_device_handle(db, device_id)
            except NoResultFound:
                _complete(job, note="device no longer exists", error=None)
                return None
            device = locked.device
            if in_maintenance(device):
                _complete(job, note="device is in maintenance", error=None)
                return None
            if (
                device.device_checks_healthy is not False
                or device.failure_episode_id != failure_episode_id
                or not link_repair.is_repeat_safe_remediation_action(action)
            ):
                _complete(job, note="device recovered or episode superseded", error=None)
                return None
            host = device.host
            connection_target = device.connection_target
            if host is None or connection_target is None:
                raise ValueError("device health remediation requires a host and connection_target")

            attempt = await _reserve_repair_attempt(db, job, device)
            if attempt is None:
                await record_event(
                    db,
                    device.id,
                    DeviceEventType.repair_failed,
                    {"action": action, "reason": "attempt budget exhausted"},
                )
                metrics.record_device_repair_attempt(action=action, outcome="budget_exhausted")
                _complete(job, note="budget exhausted", error=None)
                return None

            node = device.appium_node
            claimed_ports = (
                (await appium_node_resource_service.get_port_claims_for_nodes(db, node_ids=[node.id])).get(node.id, {})
                if node is not None
                else {}
            )
            # Driver-agnostic facts, gathered under the same Device proof that
            # justified the dispatch. ``operation_id`` is Job.id: the agent sees
            # the same value on every replay of a crashed run.
            extra_args: dict[str, Any] = {
                "operation_id": str(job.id),
                "has_live_session": await device_has_running_session(db, device.id),
                "host_has_live_sessions": await _host_has_live_sessions(db, device),
            }
            if claimed_ports:
                extra_args["claimed_ports"] = dict(claimed_ports)
            return RemediationEffect(
                job_id=job_id,
                claim_attempt=claim_attempt,
                device_id=device.id,
                failure_episode_id=failure_episode_id,
                action=action,
                repair_attempt=attempt,
                host_ip=host.ip,
                host_agent_port=host.agent_port,
                connection_target=connection_target,
                pack_id=device.pack_id,
                platform_id=device.platform_id,
                ip_address=device.ip_address,
                extra_args=extra_args,
            )

    async def _dispatch(self, effect: RemediationEffect) -> RemediationResult:
        """Run the repeat-safe action on the agent. No session, no lock, no transaction."""
        try:
            raw = await pack_device_lifecycle_action(
                effect.host_ip,
                effect.host_agent_port,
                effect.connection_target,
                pack_id=effect.pack_id,
                platform_id=effect.platform_id,
                action=effect.action,
                args={"ip_address": effect.ip_address, **effect.extra_args},
                http_client_factory=httpx.AsyncClient,
                circuit_breaker=self._circuit_breaker,
                pool=self._pool,
            )
        except AgentCallError:
            return RemediationResult(success=False, detail="")
        return RemediationResult(
            success=bool(raw.get("success")),
            detail=str(raw.get("detail") or "")[:DETAIL_MAX_LEN],
        )

    async def _finalize(self, effect: RemediationEffect, result: RemediationResult) -> str:
        async with self._session_factory.begin() as db:
            job = (await db.execute(select(Job).where(Job.id == effect.job_id).with_for_update())).scalar_one_or_none()
            if job is None:
                return "stale"
            try:
                locked = await device_locking.lock_device_handle(db, effect.device_id)
            except NoResultFound:
                return "stale"
            if (
                job.status != JOB_STATUS_RUNNING
                or job.attempts != effect.claim_attempt
                or locked.device.failure_episode_id != effect.failure_episode_id
                or locked.device.device_checks_healthy is not False
            ):
                return "stale"
            await record_event(
                db,
                locked.device.id,
                DeviceEventType.repair_attempted,
                {
                    "action": effect.action,
                    "attempt": effect.repair_attempt,
                    "success": result.success,
                    "detail": result.detail,
                },
            )
            metrics.record_device_repair_attempt(
                action=effect.action, outcome="success" if result.success else "failed"
            )
            _complete(job, note=f"dispatched {effect.action} (success={result.success})", error=None)
        return "completed"

    async def _fail_claim(self, job_id: uuid.UUID, claim_attempt: int) -> None:
        """Fail the job only while this claim still owns it; never a newer claimant's."""
        async with self._session_factory.begin() as db:
            job = await _lock_claimed_job(db, job_id, claim_attempt)
            if job is None:
                return
            _complete(job, note=None, error="remediation job crashed")

    async def run_device_health_remediation_job(
        self,
        job_id: str,
        payload: dict[str, Any],
        *,
        claim_attempt: int,
    ) -> None:
        try:
            parsed_job_id = uuid.UUID(job_id)
        except TypeError, ValueError, AttributeError:
            logger.exception("device_health_remediation: invalid job id %r", job_id)
            return

        try:
            effect = await self._prepare(parsed_job_id, payload, claim_attempt)
            if effect is None:
                return
            result = await self._dispatch(effect)
            await self._finalize(effect, result)
        except Exception:
            logger.exception("device_health_remediation: job %s crashed", job_id)
            await self._fail_claim(parsed_job_id, claim_attempt)
