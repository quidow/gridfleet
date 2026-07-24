"""Durable run-session teardown on the shared Job queue.

Cancel / expire / force-release all abort a live run: they must DELETE the
run's live Appium sessions and (for force-release) probe which genuinely
survived, then terminalize the run, close the closed sessions and release the
reservations. The remote Appium teardown MUST NOT run under an open
transaction, so the flow is split into three durable phases keyed by a single
``Job.id`` operation token:

* ``prepare`` — one transaction: lock the run, reject/no-op exactly like the old
  command, dedup on an existing pending/running teardown Job for the run, snapshot
  the immutable session targets, and enqueue the Job. It does NOT terminalize the
  run or release reservations — the active run/reservations are retained so a
  crash before the effect can never leave a device allocatable before its Appium
  session is actually torn down.
* ``perform_run_teardown_effect`` — no DB session: DELETE each snapshotted target
  (per-host concurrency bound) and, for force-release, probe survivors.
* ``finalize`` — one retried transaction: re-lock Job -> Run -> sorted Devices ->
  children, apply the run's terminal fields/events, close pending + successfully
  terminated sessions (leave failed ordinary DELETE rows live), apply forced
  hard-stops only to genuine survivors, release reservations and complete the Job.

A scheduler crash leaves the pending/running Job for the durable worker, whose
``run_run_session_teardown_job`` replays the effect (harmless if repeated) and
finalize (idempotent once).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.concurrency import per_key_semaphores
from app.core.db_retry import retry_on_serialization_failure
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.grid import appium_direct
from app.jobs.kinds import JOB_KIND_RUN_SESSION_TEARDOWN
from app.jobs.models import Job
from app.jobs.queue import create_job
from app.jobs.statuses import (
    JOB_STATUS_COMPLETED,
    JOB_STATUS_FAILED,
    JOB_STATUS_PENDING,
    JOB_STATUS_RUNNING,
)
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_lifecycle_release import (
    SURVIVAL_PROBE_RETRY_DELAY_SEC,
    TERMINATE_CONCURRENCY_PER_HOST,
    _resolve_session_target,
)
from app.runs.service_reservation import get_run_for_update
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    from collections import defaultdict

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.runs.service_lifecycle_release import RunReleaseService

logger = get_logger(__name__)


class RunTeardownKind(StrEnum):
    cancel = "cancel"
    expire = "expire"
    force_release = "force_release"


@dataclass(frozen=True, slots=True)
class SessionTeardownTarget:
    session_pk: uuid.UUID
    device_id: uuid.UUID
    appium_session_id: str
    target: str | None
    host_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class RunTeardownEffect:
    operation_id: uuid.UUID
    run_id: uuid.UUID
    kind: RunTeardownKind
    expected_state: RunState
    reason: str | None
    targets: tuple[SessionTeardownTarget, ...]


@dataclass(frozen=True, slots=True)
class RunTeardownResult:
    terminated_session_ids: frozenset[uuid.UUID]
    survivor_device_ids: frozenset[uuid.UUID]


def _payload(effect: RunTeardownEffect) -> dict[str, Any]:
    return {
        "operation_id": str(effect.operation_id),
        "run_id": str(effect.run_id),
        "kind": effect.kind.value,
        "expected_state": effect.expected_state.value,
        "reason": effect.reason,
        "targets": [
            {
                "session_pk": str(target.session_pk),
                "device_id": str(target.device_id),
                "appium_session_id": target.appium_session_id,
                "target": target.target,
                "host_id": str(target.host_id) if target.host_id is not None else None,
            }
            for target in effect.targets
        ],
    }


def _effect(payload: dict[str, Any]) -> RunTeardownEffect:
    try:
        targets = tuple(
            SessionTeardownTarget(
                session_pk=uuid.UUID(str(raw["session_pk"])),
                device_id=uuid.UUID(str(raw["device_id"])),
                appium_session_id=str(raw["appium_session_id"]),
                target=(str(raw["target"]) if raw.get("target") is not None else None),
                host_id=(uuid.UUID(str(raw["host_id"])) if raw.get("host_id") is not None else None),
            )
            for raw in payload["targets"]
        )
        return RunTeardownEffect(
            operation_id=uuid.UUID(str(payload["operation_id"])),
            run_id=uuid.UUID(str(payload["run_id"])),
            kind=RunTeardownKind(str(payload["kind"])),
            expected_state=RunState(str(payload["expected_state"])),
            reason=payload["reason"],
            targets=targets,
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"Malformed run teardown payload: {exc}") from exc


async def perform_run_teardown_effect(effect: RunTeardownEffect) -> RunTeardownResult:
    semaphores: defaultdict[uuid.UUID | None, asyncio.Semaphore] = per_key_semaphores(TERMINATE_CONCURRENCY_PER_HOST)

    async def terminate(target: SessionTeardownTarget) -> tuple[SessionTeardownTarget, bool]:
        if target.target is None:
            return target, False
        async with semaphores[target.host_id]:
            return target, await appium_direct.terminate_session(target.target, target.appium_session_id)

    outcomes = await asyncio.gather(*(terminate(target) for target in effect.targets))
    terminated = frozenset(target.session_pk for target, ok in outcomes if ok)
    survivors: set[uuid.UUID] = set()
    if effect.kind == RunTeardownKind.force_release:
        for target, _ in outcomes:
            if target.target is None:
                survivors.add(target.device_id)
                continue
            verdict = await appium_direct.session_alive(target.target, target.appium_session_id)
            if verdict is None:
                await asyncio.sleep(SURVIVAL_PROBE_RETRY_DELAY_SEC)
                verdict = await appium_direct.session_alive(target.target, target.appium_session_id)
            if verdict is not False:
                survivors.add(target.device_id)
    return RunTeardownResult(terminated, frozenset(survivors))


_RUN_EVENT: dict[RunTeardownKind, tuple[str, dict[str, str], EventSeverity]] = {
    RunTeardownKind.cancel: ("run.cancelled", {"cancelled_by": "user"}, "warning"),
    RunTeardownKind.force_release: ("run.cancelled", {"cancelled_by": "admin (force release)"}, "warning"),
}
_CALLER = {
    RunTeardownKind.cancel: "run_cancel",
    RunTeardownKind.expire: "run_expire",
    RunTeardownKind.force_release: "run_force_release",
}


class RunTeardownService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        release: RunReleaseService,
        session_factory: SessionFactory,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._release = release
        self._session_factory = session_factory

    async def teardown_run(
        self,
        kind: RunTeardownKind,
        run_id: uuid.UUID,
        reason: str | None = None,
    ) -> None:
        effect = await self.prepare(kind, run_id, reason)
        if effect is not None:
            result = await perform_run_teardown_effect(effect)
            await self._finalize_and_stop(effect, result)

    async def run_run_session_teardown_job(self, job_id: str, payload: dict[str, Any]) -> None:
        parsed_job_id = uuid.UUID(job_id)
        try:
            effect = _effect(payload)
        except ValueError:
            logger.exception("run_session_teardown: malformed payload for job %s", job_id)
            await self._fail_job(parsed_job_id, "malformed run teardown payload")
            return
        try:
            result = await perform_run_teardown_effect(effect)
            await self._finalize_and_stop(effect, result)
        except Exception:
            logger.exception("run_session_teardown: job %s for run %s crashed", job_id, effect.run_id)
            await self._fail_job(parsed_job_id, "run_session_teardown job crashed unexpectedly")

    async def prepare(
        self,
        kind: RunTeardownKind,
        run_id: uuid.UUID,
        reason: str | None,
    ) -> RunTeardownEffect | None:
        return await retry_on_serialization_failure(
            self._session_factory, lambda db: self._prepare_txn(db, kind, run_id, reason), caller="run_teardown"
        )

    async def _prepare_txn(
        self,
        db: AsyncSession,
        kind: RunTeardownKind,
        run_id: uuid.UUID,
        reason: str | None,
    ) -> RunTeardownEffect | None:
        run = await get_run_for_update(db, run_id)
        if run is None:
            if kind == RunTeardownKind.expire:
                return None
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            if kind == RunTeardownKind.cancel:
                raise ValueError(f"Run is already in terminal state '{run.state.value}'")
            return None

        existing = await db.scalar(
            select(Job)
            .where(
                Job.kind == JOB_KIND_RUN_SESSION_TEARDOWN,
                Job.status.in_((JOB_STATUS_PENDING, JOB_STATUS_RUNNING)),
                Job.payload["run_id"].astext == str(run_id),
            )
            .with_for_update()
        )
        if existing is not None:
            return _effect(existing.payload)

        expected_state = run.state
        locked_by_id = await self._release.lock_run_devices(db, run)
        devices_by_id = {locked.device.id: locked.device for locked in locked_by_id.values()}
        sessions = (
            (await db.execute(select(Session).where(Session.run_id == run_id, live_session_predicate())))
            .scalars()
            .all()
        )
        targets: list[SessionTeardownTarget] = []
        for session in sessions:
            if session.device_id is None:
                continue
            target = (
                _resolve_session_target(session, devices_by_id) if session.status == SessionStatus.running else None
            )
            device = devices_by_id.get(session.device_id)
            targets.append(
                SessionTeardownTarget(
                    session_pk=session.id,
                    device_id=session.device_id,
                    appium_session_id=session.session_id,
                    target=target,
                    host_id=device.host_id if device is not None else None,
                )
            )

        operation_id = uuid.uuid4()
        effect = RunTeardownEffect(
            operation_id=operation_id,
            run_id=run_id,
            kind=kind,
            expected_state=expected_state,
            reason=reason,
            targets=tuple(targets),
        )
        await create_job(
            db,
            kind=JOB_KIND_RUN_SESSION_TEARDOWN,
            payload=_payload(effect),
            snapshot={"operation_id": str(operation_id), "status": JOB_STATUS_PENDING},
            job_id=operation_id,
            commit=False,
            max_attempts=3,
        )
        return effect

    async def finalize(self, effect: RunTeardownEffect, result: RunTeardownResult) -> list[uuid.UUID]:
        return await retry_on_serialization_failure(
            self._session_factory, lambda db: self._finalize_txn(db, effect, result), caller="run_teardown"
        )

    async def _finalize_txn(
        self,
        db: AsyncSession,
        effect: RunTeardownEffect,
        result: RunTeardownResult,
    ) -> list[uuid.UUID]:
        job = await db.scalar(select(Job).where(Job.id == effect.operation_id).with_for_update())
        if job is None or job.status in (JOB_STATUS_COMPLETED, JOB_STATUS_FAILED):
            return []
        if job.payload.get("operation_id") != str(effect.operation_id):
            return []

        run = await get_run_for_update(db, effect.run_id)
        # ``expected_state`` is captured at prepare; a non-terminal advance
        # (preparing -> active) must still let cancel/expire/force apply, so the
        # only superseding condition is a run already terminalized by a competing
        # terminal operation (complete or another teardown).
        if run is None or run.state in TERMINAL_STATES:
            self._complete_job(job, result, note="superseded")
            return []

        locked_by_id = await self._release.lock_run_devices(db, run)
        caller = _CALLER[effect.kind]
        survivors = set(result.survivor_device_ids) if effect.kind == RunTeardownKind.force_release else None
        effective_reason = self._apply_run_terminal(run, effect)
        await self._release.clear_desired_grid_run_id_for_run(
            db,
            run=run,
            caller=caller,
            locked_by_id=locked_by_id,
            reason=effective_reason,
            stop_device_ids=survivors,
        )
        cleanup_ids = await self._release.release_devices(
            db, run, locked_by_id=locked_by_id, close_session_ids=result.terminated_session_ids
        )
        self._queue_run_events(db, run, effect, effective_reason)
        self._complete_job(job, result, note="completed")
        return cleanup_ids

    def _apply_run_terminal(self, run: TestRun, effect: RunTeardownEffect) -> str | None:
        now = now_utc()
        if effect.kind == RunTeardownKind.cancel:
            run.state = RunState.cancelled
            run.completed_at = now
            return None
        if effect.kind == RunTeardownKind.force_release:
            run.state = RunState.cancelled
            run.error = "Force released by admin"
            run.completed_at = now
            return None
        # expire
        effective_reason = effect.reason or ""
        if effect.expected_state == RunState.preparing:
            effective_reason = (
                f"{effect.reason}; run was still in `preparing` — `/api/runs/{{id}}/active` was never signaled"
            )
        run.state = RunState.expired
        run.error = effective_reason
        run.completed_at = now
        return effective_reason

    def _queue_run_events(
        self,
        db: AsyncSession,
        run: TestRun,
        effect: RunTeardownEffect,
        effective_reason: str | None,
    ) -> None:
        if effect.kind in _RUN_EVENT:
            name, extra, severity = _RUN_EVENT[effect.kind]
            self._publisher.queue_for_session(
                db,
                name,
                {"run_id": str(run.id), "name": run.name, **extra},
                severity=severity,
            )
            return
        # expire
        if effect.expected_state == RunState.preparing:
            self._publisher.queue_for_session(
                db,
                "run.never_activated",
                {"run_id": str(run.id), "name": run.name, "reason": effective_reason},
                severity="warning",
            )
        self._publisher.queue_for_session(
            db,
            "run.expired",
            {"run_id": str(run.id), "name": run.name, "reason": effective_reason},
            severity="critical",
        )

    @staticmethod
    def _complete_job(job: Job, result: RunTeardownResult, *, note: str) -> None:
        snapshot = dict(job.snapshot)
        snapshot["status"] = JOB_STATUS_COMPLETED
        snapshot["note"] = note
        snapshot["result"] = {
            "terminated_session_ids": sorted(str(sid) for sid in result.terminated_session_ids),
            "survivor_device_ids": sorted(str(did) for did in result.survivor_device_ids),
        }
        snapshot["finished_at"] = now_utc().isoformat()
        job.snapshot = snapshot
        job.status = JOB_STATUS_COMPLETED
        job.completed_at = now_utc()

    async def _finalize_and_stop(self, effect: RunTeardownEffect, result: RunTeardownResult) -> None:
        cleanup_ids = await self.finalize(effect, result)
        await self._run_deferred_stops(cleanup_ids)

    async def _run_deferred_stops(self, device_ids: list[uuid.UUID]) -> None:
        if not device_ids:
            return
        async with self._session_factory() as db:
            await self._release.complete_deferred_stops_post_commit(db, device_ids)

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
