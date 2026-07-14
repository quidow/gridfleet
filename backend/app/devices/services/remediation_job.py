"""Durable worker for repeat-safe device-health remediation actions."""

from __future__ import annotations

import copy
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound

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
from app.jobs import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED
from app.jobs.models import Job
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session
from app.sessions.service import device_has_running_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.devices.protocols import DeviceHealthProtocol

logger = get_logger(__name__)


async def _host_has_live_sessions(db: AsyncSession, device: Device) -> bool:
    row = await db.execute(
        select(Session.id)
        .join(Device, Session.device_id == Device.id)
        .where(Device.host_id == device.host_id, live_session_predicate())
        .limit(1)
    )
    return row.first() is not None


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

    @staticmethod
    async def _finalize(
        db: AsyncSession,
        job_id: uuid.UUID,
        *,
        note: str | None,
        error: str | None,
    ) -> None:
        row = await db.get(Job, job_id)
        if row is None:
            return
        status = JOB_STATUS_FAILED if error is not None else JOB_STATUS_COMPLETED
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
        await db.commit()

    async def _run(
        self,
        db: AsyncSession,
        job_id: uuid.UUID,
        device_id: uuid.UUID,
        failure_episode_id: uuid.UUID,
        action: str,
    ) -> None:
        try:
            device = await device_locking.lock_device(db, device_id)
        except NoResultFound:
            await self._finalize(db, job_id, note="device no longer exists", error=None)
            return
        if in_maintenance(device):
            await self._finalize(db, job_id, note="device is in maintenance", error=None)
            return
        if (
            device.device_checks_healthy is not False
            or device.failure_episode_id != failure_episode_id
            or not link_repair.is_repeat_safe_remediation_action(action)
        ):
            await self._finalize(db, job_id, note="device recovered or episode superseded", error=None)
            return

        attempt = await link_repair.next_repair_attempt(db, device.identity_value)
        if attempt is None:
            await record_event(
                db,
                device.id,
                DeviceEventType.repair_failed,
                {"action": action, "reason": "attempt budget exhausted"},
            )
            metrics.record_device_repair_attempt(action=action, outcome="budget_exhausted")
            await self._finalize(db, job_id, note="budget exhausted", error=None)
            return

        has_live_session = await device_has_running_session(db, device.id)
        host_has_live_sessions = await _host_has_live_sessions(db, device)
        node = device.appium_node
        claimed_ports = (
            (await appium_node_resource_service.get_port_claims_for_nodes(db, node_ids=[node.id])).get(node.id, {})
            if node is not None
            else {}
        )
        extra_args: dict[str, Any] = {
            "has_live_session": has_live_session,
            "host_has_live_sessions": host_has_live_sessions,
        }
        if claimed_ports:
            extra_args["claimed_ports"] = claimed_ports

        # The action is repeat-safe, so a crash after dispatch can safely retry.
        # Commit the attempt reservation after snapshotting the fresh dispatch
        # facts, releasing the device row lock before the slow agent request.
        await db.commit()
        try:
            result = await link_repair.dispatch_recommended_action(
                device,
                action,
                circuit_breaker=self._circuit_breaker,
                pool=self._pool,
                extra_args=extra_args,
            )
        except AgentCallError:
            result = {"success": False}

        success = bool(result.get("success"))
        await record_event(
            db,
            device.id,
            DeviceEventType.repair_attempted,
            {
                "action": action,
                "attempt": attempt,
                "success": success,
                "detail": str(result.get("detail") or "")[:200],
            },
        )
        metrics.record_device_repair_attempt(action=action, outcome="success" if success else "failed")
        await db.commit()
        await self._finalize(db, job_id, note=f"dispatched {action} (success={success})", error=None)

    async def run_device_health_remediation_job(self, job_id: str, payload: dict[str, Any]) -> None:
        try:
            parsed_job_id = uuid.UUID(job_id)
        except TypeError, ValueError, AttributeError:
            logger.exception("device_health_remediation: invalid job id %r", job_id)
            return

        try:
            device_id = uuid.UUID(str(payload["device_id"]))
            failure_episode_id = uuid.UUID(str(payload["failure_episode_id"]))
            action = str(payload["action_id"])
            async with self._session_factory() as db:
                await self._run(db, parsed_job_id, device_id, failure_episode_id, action)
        except Exception:
            logger.exception("device_health_remediation: job %s crashed", job_id)
            async with self._session_factory() as db:
                await self._finalize(db, parsed_job_id, note=None, error="remediation job crashed")
