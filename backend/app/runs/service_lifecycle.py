from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.core.db_retry import retry_on_serialization_failure
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_reservation import get_run
from app.runs.service_reservation import get_run_for_update as _get_run_for_update

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.catalog import EventSeverity
    from app.events.protocols import EventPublisher
    from app.runs.protocols import RunReleaseProtocol

# A terminal run transition touches the run row, every reserved device row and
# their session rows in one transaction, racing teardown traffic on the same
# rows (terminal session-status writes, background reconciles). Postgres
# resolves such a collision by killing one transaction with a deadlock error
# (sqlstate 40P01). Failing the transition over a transient loser pick leaks
# the run's reservations until the reaper expires them — starving allocation
# for the whole heartbeat-timeout window — so roll back and re-run the
# transition a bounded number of times before surfacing.


def _run_completed_severity(run: TestRun) -> EventSeverity:
    """Return 'success' for a clean completion, 'warning' if any session failed."""
    # run.error is set when the run completed due to an internal error or
    # partial failure (e.g. some sessions were in a failed/error state).
    if run.error:
        return "warning"
    return "success"


class RunLifecycleService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        release: RunReleaseProtocol,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._release = release

    async def signal_ready(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state != RunState.preparing:
            raise ValueError(f"Cannot signal ready from state '{run.state.value}', expected 'preparing'")

        now = datetime.now(UTC)
        run.state = RunState.active
        run.started_at = now
        run.last_heartbeat = now
        self._publisher.queue_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
        await db.commit()
        run = await get_run(db, run_id)
        assert run is not None
        return run

    async def signal_active(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state == RunState.active:
            await db.commit()
            return run
        if run.state != RunState.preparing:
            raise ValueError(f"Cannot signal active from state '{run.state.value}', expected 'preparing' or 'active'")

        now = datetime.now(UTC)
        run.state = RunState.active
        run.started_at = now
        run.last_heartbeat = now
        self._publisher.queue_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
        await db.commit()
        run = await get_run(db, run_id)
        assert run is not None
        return run

    async def heartbeat(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            await db.commit()
            return run

        run.last_heartbeat = datetime.now(UTC)
        await db.commit()
        run = await get_run(db, run_id)
        assert run is not None
        return run

    async def _retry_on_deadlock(
        self, db: AsyncSession, attempt_txn: Callable[[], Awaitable[list[uuid.UUID]]]
    ) -> list[uuid.UUID]:
        """Run one terminal-transition transaction, retrying serialization losses.

        ``attempt_txn`` must own the whole transaction (re-read the run under
        lock, mutate, commit); the shared helper rolls back between attempts.
        """
        return await retry_on_serialization_failure(db, attempt_txn, caller="run_lifecycle")

    async def complete_run(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        cleanup_ids = await self._retry_on_deadlock(db, lambda: self._complete_run_txn(db, run_id))
        await self._release.complete_deferred_stops_post_commit(db, cleanup_ids)
        run = await get_run(db, run_id)
        assert run is not None
        return run

    async def _complete_run_txn(self, db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            raise ValueError(f"Run is already in terminal state '{run.state.value}'")

        now = datetime.now(UTC)
        await self._release.clear_desired_grid_run_id_for_run(db, run=run, caller="run_complete")
        run.state = RunState.completed
        run.completed_at = now
        cleanup_ids = await self._release.release_devices(db, run, commit=False, terminate_grid_sessions=False)

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
        await db.commit()
        return cleanup_ids

    async def cancel_run(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        cleanup_ids = await self._retry_on_deadlock(db, lambda: self._cancel_run_txn(db, run_id))
        await self._release.complete_deferred_stops_post_commit(db, cleanup_ids)
        run = await get_run(db, run_id)
        assert run is not None
        return run

    async def _cancel_run_txn(self, db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")
        if run.state in TERMINAL_STATES:
            raise ValueError(f"Run is already in terminal state '{run.state.value}'")

        await self._release.clear_desired_grid_run_id_for_run(db, run=run, caller="run_cancel")
        run.state = RunState.cancelled
        run.completed_at = datetime.now(UTC)
        cleanup_ids = await self._release.release_devices(db, run, commit=False, terminate_grid_sessions=True)
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
        await db.commit()
        return cleanup_ids

    async def force_release(self, db: AsyncSession, run_id: uuid.UUID) -> TestRun:
        cleanup_ids = await self._retry_on_deadlock(db, lambda: self._force_release_txn(db, run_id))
        await self._release.complete_deferred_stops_post_commit(db, cleanup_ids)
        run = await get_run(db, run_id)
        assert run is not None
        return run

    async def _force_release_txn(self, db: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
        run = await _get_run_for_update(db, run_id)
        if run is None:
            raise ValueError("Run not found")

        await self._release.clear_desired_grid_run_id_for_run(db, run=run, caller="run_force_release")
        run.state = RunState.cancelled
        run.error = "Force released by admin"
        run.completed_at = datetime.now(UTC)
        cleanup_ids = await self._release.release_devices(db, run, commit=False, terminate_grid_sessions=True)
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
        await db.commit()
        return cleanup_ids

    async def expire_run(self, db: AsyncSession, run: TestRun, reason: str) -> None:
        """Expire a run due to heartbeat or TTL timeout. Called by the reaper."""

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

        await self._release.clear_desired_grid_run_id_for_run(
            db, run=locked_run, caller="run_expire", reason=effective_reason
        )
        locked_run.state = RunState.expired
        locked_run.error = effective_reason
        locked_run.completed_at = datetime.now(UTC)
        cleanup_ids = await self._release.release_devices(db, locked_run, commit=False, terminate_grid_sessions=True)

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
