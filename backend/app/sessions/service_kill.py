"""Operator-initiated session kill on the durable Job queue.

Best-effort DELETE of the live Appium session, then unconditional terminalization
of the DB row as ``error``/``operator_kill`` — even when the DELETE fails or no
target resolves, so the row still leaves the live set (the condition under which
the ``session_sync`` orphan sweep kills any still-alive Appium session on the
next tick — no zombie path).

The remote DELETE never runs under an open transaction. The flow is durable,
keyed by a single ``Job.id`` operation token:

* ``prepare`` — one transaction: lock the Device then the running Session child,
  reuse an existing pending/running ``session_kill`` Job for the same session, or
  enqueue one snapshotting the immutable Appium target.
* ``_perform_kill_effect`` — no DB session: DELETE the Appium session.
* ``finalize`` — one transaction: re-lock Job -> Device -> Session, close through
  ``close_running_session_locked`` with the operator-kill attribution regardless
  of the DELETE result, record ``terminated`` and complete the Job. A concurrent
  natural end wins without duplicate events (the locked close is idempotent).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.schemas.device import SessionRead
from app.grid import appium_direct
from app.grid.allocation import resolve_router_target
from app.jobs.kinds import JOB_KIND_SESSION_KILL
from app.jobs.models import Job
from app.jobs.queue import create_job
from app.jobs.statuses import JOB_STATUS_COMPLETED, JOB_STATUS_FAILED, JOB_STATUS_PENDING, JOB_STATUS_RUNNING
from app.sessions.models import Session, SessionStatus
from app.sessions.service import close_running_session_locked

if TYPE_CHECKING:
    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)

OPERATOR_KILL_ERROR_TYPE = "operator_kill"
OPERATOR_KILL_ERROR_MESSAGE = "killed by operator"


class SessionNotKillableError(Exception):
    """The session exists but is not a live running session."""


@dataclass(frozen=True, slots=True)
class KillEffect:
    operation_id: uuid.UUID
    session_pk: uuid.UUID
    device_id: uuid.UUID
    appium_session_id: str
    target: str | None


@dataclass(frozen=True, slots=True)
class KillOutcome:
    session: SessionRead
    terminated: bool


def _payload(effect: KillEffect) -> dict[str, Any]:
    return {
        "operation_id": str(effect.operation_id),
        "session_pk": str(effect.session_pk),
        "device_id": str(effect.device_id),
        "appium_session_id": effect.appium_session_id,
        "target": effect.target,
    }


def _effect(payload: dict[str, Any]) -> KillEffect:
    try:
        return KillEffect(
            operation_id=uuid.UUID(str(payload["operation_id"])),
            session_pk=uuid.UUID(str(payload["session_pk"])),
            device_id=uuid.UUID(str(payload["device_id"])),
            appium_session_id=str(payload["appium_session_id"]),
            target=(str(payload["target"]) if payload.get("target") is not None else None),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"Malformed session kill payload: {exc}") from exc


async def _perform_kill_effect(effect: KillEffect) -> bool:
    if effect.target is None:
        return False
    # The Appium URL is built from a stored target, never raw request input.
    return await appium_direct.terminate_session(effect.target, effect.appium_session_id)


class SessionKillService:
    def __init__(self, *, publisher: EventPublisher, session_factory: SessionFactory) -> None:
        self._publisher = publisher
        self._session_factory = session_factory

    async def kill(self, session_id: str) -> KillOutcome | None:
        """Returns None for an unknown session; raises SessionNotKillableError for
        a row that is not running (pending rows belong to the allocation reaper)."""
        effect = await self.prepare(session_id)
        if effect is None:
            return None
        terminated = await _perform_kill_effect(effect)
        return await self.finalize(effect, terminated)

    async def run_session_kill_job(self, job_id: str, payload: dict[str, Any]) -> None:
        parsed_job_id = uuid.UUID(job_id)
        try:
            effect = _effect(payload)
        except ValueError:
            logger.exception("session_kill: malformed payload for job %s", job_id)
            await self._fail_job(parsed_job_id, "malformed session kill payload")
            return
        try:
            terminated = await _perform_kill_effect(effect)
            await self.finalize(effect, terminated)
        except Exception:
            logger.exception("session_kill: job %s for session %s crashed", job_id, effect.session_pk)
            await self._fail_job(parsed_job_id, "session_kill job crashed unexpectedly")

    async def prepare(self, session_id: str) -> KillEffect | None:
        async with self._session_factory.begin() as db:
            stmt = (
                select(Session)
                .where(Session.session_id == session_id)
                .options(
                    selectinload(Session.device).selectinload(Device.appium_node),
                    selectinload(Session.device).selectinload(Device.host),
                )
                .order_by(Session.started_at.desc(), Session.id.desc())
                .limit(1)
            )
            session = (await db.execute(stmt)).scalars().first()
            if session is None:
                return None
            if session.status != SessionStatus.running or session.ended_at is not None:
                raise SessionNotKillableError(session_id)
            if session.device_id is None:
                raise SessionNotKillableError(session_id)

            target = resolve_router_target(session)
            session_pk = session.id
            device_id = session.device_id
            appium_session_id = session.session_id

            # Device -> Session lock order.
            await device_locking.lock_device(db, device_id)
            await db.scalar(select(Session).where(Session.id == session_pk).with_for_update())

            existing = await db.scalar(
                select(Job)
                .where(
                    Job.kind == JOB_KIND_SESSION_KILL,
                    Job.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING)),
                    Job.payload["session_pk"].astext == str(session_pk),
                )
                .with_for_update()
            )
            if existing is not None:
                return _effect(existing.payload)

            operation_id = uuid.uuid4()
            effect = KillEffect(
                operation_id=operation_id,
                session_pk=session_pk,
                device_id=device_id,
                appium_session_id=appium_session_id,
                target=target,
            )
            await create_job(
                db,
                kind=JOB_KIND_SESSION_KILL,
                payload=_payload(effect),
                snapshot={"operation_id": str(operation_id), "status": JOB_STATUS_PENDING},
                job_id=operation_id,
                commit=False,
                max_attempts=3,
            )
            return effect

    async def finalize(self, effect: KillEffect, terminated: bool) -> KillOutcome:
        async with self._session_factory.begin() as db:
            job = await db.scalar(select(Job).where(Job.id == effect.operation_id).with_for_update())
            already_done = job is not None and job.status in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED)

            try:
                locked = await device_locking.lock_device_handle(db, effect.device_id)
            except NoResultFound:
                locked = None
            if locked is not None:
                await close_running_session_locked(
                    db,
                    locked,
                    session_pk=effect.session_pk,
                    publisher=self._publisher,
                    status_override=SessionStatus.error,
                    error_type=OPERATOR_KILL_ERROR_TYPE,
                    error_message=OPERATOR_KILL_ERROR_MESSAGE,
                )

            row = await db.scalar(select(Session).where(Session.id == effect.session_pk))
            if row is None:
                raise SessionNotKillableError(str(effect.session_pk))

            terminated_value = bool(job.snapshot.get("terminated")) if already_done and job is not None else terminated
            if job is not None and not already_done:
                snapshot = dict(job.snapshot)
                snapshot["status"] = JOB_STATUS_COMPLETED
                snapshot["terminated"] = terminated
                snapshot["finished_at"] = now_utc().isoformat()
                job.snapshot = snapshot
                job.status = JOB_STATUS_COMPLETED
                job.completed_at = now_utc()

            return KillOutcome(session=SessionRead.model_validate(row), terminated=terminated_value)

    async def _fail_job(self, operation_id: uuid.UUID, error: str) -> None:
        async with self._session_factory.begin() as db:
            job = await db.get(Job, operation_id)
            if job is None:
                return
            snapshot = dict(job.snapshot)
            snapshot["status"] = JOB_STATUS_FAILED
            snapshot["error"] = error
            snapshot["finished_at"] = now_utc().isoformat()
            job.snapshot = snapshot
            job.status = JOB_STATUS_FAILED
            job.completed_at = now_utc()
