from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.db_retry import retry_on_serialization_failure
from app.core.timeutil import now_utc
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_reservation import get_run_for_update as _get_run_for_update

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.node_poke import NodeRefreshTarget
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.runs.service_lifecycle_release import RunReleaseService

# A terminal run transition touches the run row, every reserved device row and
# their session rows in one transaction, racing teardown traffic on the same
# rows (terminal session-status writes, background reconciles). Postgres
# resolves such a collision by killing one transaction with a deadlock error
# (sqlstate 40P01). Failing the transition over a transient loser pick leaks
# the run's reservations until the reaper expires them — starving allocation
# for the whole heartbeat-timeout window — so open a fresh session and re-run
# the whole transition a bounded number of times before surfacing.


@dataclass(frozen=True, slots=True)
class RunCommandResult:
    run_id: uuid.UUID
    wake_targets: tuple[NodeRefreshTarget, ...] = ()


def _run_completed_severity(run: TestRun) -> EventSeverity:
    """Return 'success' for a clean completion, 'warning' if any session failed."""
    if run.error:
        return "warning"
    return "success"


class RunLifecycleService:
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

    async def signal_ready(self, run_id: uuid.UUID) -> RunCommandResult:
        return await retry_on_serialization_failure(
            self._session_factory, lambda db: self._signal_ready_txn(db, run_id), caller="run_lifecycle"
        )

    async def _signal_ready_txn(self, db: AsyncSession, run_id: uuid.UUID) -> RunCommandResult:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state != RunState.preparing:
            raise ValueError(f"Cannot signal ready from state '{run.state.value}', expected 'preparing'")

        now = now_utc()
        run.state = RunState.active
        run.started_at = now
        run.last_heartbeat = now
        self._publisher.queue_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
        return RunCommandResult(run_id=run.id)

    async def signal_active(self, run_id: uuid.UUID) -> RunCommandResult:
        return await retry_on_serialization_failure(
            self._session_factory, lambda db: self._signal_active_txn(db, run_id), caller="run_lifecycle"
        )

    async def _signal_active_txn(self, db: AsyncSession, run_id: uuid.UUID) -> RunCommandResult:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state == RunState.active:
            return RunCommandResult(run_id=run.id)
        if run.state != RunState.preparing:
            raise ValueError(f"Cannot signal active from state '{run.state.value}', expected 'preparing' or 'active'")

        now = now_utc()
        run.state = RunState.active
        run.started_at = now
        run.last_heartbeat = now
        self._publisher.queue_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
        return RunCommandResult(run_id=run.id)

    async def heartbeat(self, run_id: uuid.UUID) -> RunCommandResult:
        return await retry_on_serialization_failure(
            self._session_factory, lambda db: self._heartbeat_txn(db, run_id), caller="run_lifecycle"
        )

    async def _heartbeat_txn(self, db: AsyncSession, run_id: uuid.UUID) -> RunCommandResult:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            return RunCommandResult(run_id=run.id)

        run.last_heartbeat = now_utc()
        return RunCommandResult(run_id=run.id)

    async def complete_run(self, run_id: uuid.UUID) -> RunCommandResult:
        cleanup_ids = await retry_on_serialization_failure(
            self._session_factory, lambda db: self._complete_run_txn(db, run_id), caller="run_lifecycle"
        )
        await self._run_deferred_stops(cleanup_ids)
        return RunCommandResult(run_id=run_id)

    async def _complete_run_txn(self, db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            raise ValueError(f"Run is already in terminal state '{run.state.value}'")

        now = now_utc()
        locked_by_id = await self._release.lock_run_devices(db, run)
        await self._release.clear_desired_grid_run_id_for_run(
            db, run=run, caller="run_complete", locked_by_id=locked_by_id
        )
        run.state = RunState.completed
        run.completed_at = now
        cleanup_ids = await self._release.release_devices(db, run, locked_by_id=locked_by_id)

        duration = None
        if run.started_at:
            duration = int((now - run.started_at).total_seconds())
        self._publisher.queue_for_session(
            db,
            "run.completed",
            {
                "run_id": str(run.id),
                "name": run.name,
                "duration": duration,
            },
            severity=_run_completed_severity(run),
        )
        return cleanup_ids

    async def cancel_run(self, run_id: uuid.UUID) -> RunCommandResult:
        cleanup_ids = await retry_on_serialization_failure(
            self._session_factory, lambda db: self._cancel_run_txn(db, run_id), caller="run_lifecycle"
        )
        await self._run_deferred_stops(cleanup_ids)
        return RunCommandResult(run_id=run_id)

    async def _cancel_run_txn(self, db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            raise ValueError(f"Run is already in terminal state '{run.state.value}'")

        locked_by_id = await self._release.lock_run_devices(db, run)
        await self._release.clear_desired_grid_run_id_for_run(
            db, run=run, caller="run_cancel", locked_by_id=locked_by_id
        )
        run.state = RunState.cancelled
        run.completed_at = now_utc()
        cleanup_ids = await self._release.release_devices(db, run, locked_by_id=locked_by_id)
        self._publisher.queue_for_session(
            db,
            "run.cancelled",
            {
                "run_id": str(run.id),
                "name": run.name,
                "cancelled_by": "user",
            },
            severity="warning",
        )
        return cleanup_ids

    async def force_release(self, run_id: uuid.UUID) -> RunCommandResult:
        cleanup_ids = await retry_on_serialization_failure(
            self._session_factory, lambda db: self._force_release_txn(db, run_id), caller="run_lifecycle"
        )
        await self._run_deferred_stops(cleanup_ids)
        return RunCommandResult(run_id=run_id)

    async def _force_release_txn(self, db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")

        # Verify-then-stop (design P3): DELETE the run's live sessions and probe
        # which genuinely survived BEFORE deciding hard-stops. Touches no
        # reservations (so clear_... still sees them active) and does NOT close
        # the session rows (release_devices closes them after run.state=cancelled).
        survivors = await self._release.terminate_run_sessions_and_probe_survivors(db, run)
        locked_by_id = await self._release.lock_run_devices(db, run)
        await self._release.clear_desired_grid_run_id_for_run(
            db, run=run, caller="run_force_release", locked_by_id=locked_by_id, stop_device_ids=survivors
        )
        run.state = RunState.cancelled
        run.error = "Force released by admin"
        run.completed_at = now_utc()
        cleanup_ids = await self._release.release_devices(db, run, locked_by_id=locked_by_id)
        self._publisher.queue_for_session(
            db,
            "run.cancelled",
            {
                "run_id": str(run.id),
                "name": run.name,
                "cancelled_by": "admin (force release)",
            },
            severity="warning",
        )
        return cleanup_ids

    async def _run_deferred_stops(self, device_ids: list[uuid.UUID]) -> None:
        if not device_ids:
            return
        async with self._session_factory() as db:
            await self._release.complete_deferred_stops_post_commit(db, device_ids)

    async def expire_run(self, db: AsyncSession, run: TestRun, reason: str) -> None:
        """Expire a run due to heartbeat or TTL timeout. Called by the reaper, which
        owns the transaction and its commit boundary."""

        locked_run = await _get_run_for_update(db, run.id)
        if locked_run is None:
            return
        if locked_run.state in TERMINAL_STATES:
            await db.commit()
            return

        expired_from_preparing = locked_run.state == RunState.preparing
        effective_reason = (
            f"{reason}; run was still in `preparing` — `/api/runs/{{id}}/active` was never signaled"
            if expired_from_preparing
            else reason
        )

        locked_by_id = await self._release.lock_run_devices(db, locked_run)
        await self._release.clear_desired_grid_run_id_for_run(
            db, run=locked_run, caller="run_expire", locked_by_id=locked_by_id, reason=effective_reason
        )
        locked_run.state = RunState.expired
        locked_run.error = effective_reason
        locked_run.completed_at = now_utc()
        cleanup_ids = await self._release.release_devices(db, locked_run, locked_by_id=locked_by_id)

        if expired_from_preparing:
            self._publisher.queue_for_session(
                db,
                "run.never_activated",
                {
                    "run_id": str(locked_run.id),
                    "name": locked_run.name,
                    "reason": effective_reason,
                },
                severity="warning",
            )

        self._publisher.queue_for_session(
            db,
            "run.expired",
            {
                "run_id": str(locked_run.id),
                "name": locked_run.name,
                "reason": effective_reason,
            },
            severity="critical",
        )
        await db.commit()
        await self._release.complete_deferred_stops_post_commit(db, cleanup_ids)
