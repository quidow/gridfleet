from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.db_retry import retry_on_serialization_failure
from app.core.timeutil import now_utc
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_reservation import get_run_for_update as _get_run_for_update
from app.runs.service_teardown import RunTeardownKind, RunTeardownService

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
        teardown: RunTeardownService | None = None,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._release = release
        self._session_factory = session_factory
        # The durable teardown collaborator is the same stateless configuration
        # the lifecycle already holds; build one when composition does not inject
        # a shared instance (keeps the wide test-construction surface untouched).
        self._teardown = teardown or RunTeardownService(
            publisher=publisher, settings=settings, release=release, session_factory=session_factory
        )

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
        await self._teardown.teardown_run(RunTeardownKind.cancel, run_id)
        return RunCommandResult(run_id=run_id)

    async def force_release(self, run_id: uuid.UUID) -> RunCommandResult:
        await self._teardown.teardown_run(RunTeardownKind.force_release, run_id)
        return RunCommandResult(run_id=run_id)

    async def _run_deferred_stops(self, device_ids: list[uuid.UUID]) -> None:
        if not device_ids:
            return
        async with self._session_factory() as db:
            await self._release.complete_deferred_stops_post_commit(db, device_ids)

    async def expire_run(self, run_id: uuid.UUID, reason: str) -> None:
        """Expire a run due to heartbeat or TTL timeout via the durable teardown
        flow (prepare -> effect -> finalize). Accepts no caller session: the reaper
        must release the run row lock before calling this so ``prepare`` can re-lock."""
        await self._teardown.teardown_run(RunTeardownKind.expire, run_id, reason)
