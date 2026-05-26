from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.events import event_bus as _default_event_bus
from app.events import queue_event_for_session
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_lifecycle_release import (
    _clear_desired_grid_run_id_for_run,
    _complete_deferred_stops_post_commit,
    _release_devices,
)
from app.runs.service_reservation import get_run
from app.runs.service_reservation import get_run_for_update as _get_run_for_update

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.events.catalog import EventSeverity
    from app.events.event_bus import EventBus


def _run_completed_severity(run: TestRun) -> EventSeverity:
    """Return 'success' for a clean completion, 'warning' if any session failed."""
    # run.error is set when the run completed due to an internal error or
    # partial failure (e.g. some sessions were in a failed/error state).
    if run.error:
        return "warning"
    return "success"


async def signal_ready(db: AsyncSession, run_id: uuid.UUID, *, publisher: EventBus | None = None) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state != RunState.preparing:
        raise ValueError(f"Cannot signal ready from state '{run.state.value}', expected 'preparing'")

    now = datetime.now(UTC)
    run.state = RunState.active
    run.started_at = now
    run.last_heartbeat = now
    queue_event_for_session(
        db, "run.active", {"run_id": str(run.id), "name": run.name}, publisher=publisher or _default_event_bus
    )
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def signal_active(db: AsyncSession, run_id: uuid.UUID, *, publisher: EventBus | None = None) -> TestRun:
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
    queue_event_for_session(
        db, "run.active", {"run_id": str(run.id), "name": run.name}, publisher=publisher or _default_event_bus
    )
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def heartbeat(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
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


async def complete_run(db: AsyncSession, run_id: uuid.UUID, *, publisher: EventBus | None = None) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Run is already in terminal state '{run.state.value}'")

    now = datetime.now(UTC)
    await _clear_desired_grid_run_id_for_run(db, run=run, caller="run_complete")
    run.state = RunState.completed
    run.completed_at = now
    cleanup_ids = await _release_devices(db, run, commit=False, terminate_grid_sessions=False)

    duration = None
    if run.started_at:
        duration = int((now - run.started_at).total_seconds())
    queue_event_for_session(
        db,
        "run.completed",
        {
            "run_id": str(run.id),
            "name": run.name,
            "duration": duration,
        },
        severity=_run_completed_severity(run),
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def cancel_run(db: AsyncSession, run_id: uuid.UUID, *, publisher: EventBus | None = None) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state in TERMINAL_STATES:
        raise ValueError(f"Run is already in terminal state '{run.state.value}'")

    await _clear_desired_grid_run_id_for_run(db, run=run, caller="run_cancel")
    run.state = RunState.cancelled
    run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, run, commit=False, terminate_grid_sessions=True)
    queue_event_for_session(
        db,
        "run.cancelled",
        {
            "run_id": str(run.id),
            "name": run.name,
            "cancelled_by": "user",
        },
        severity="warning",
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def force_release(db: AsyncSession, run_id: uuid.UUID, *, publisher: EventBus | None = None) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")

    await _clear_desired_grid_run_id_for_run(db, run=run, caller="run_force_release")
    run.state = RunState.cancelled
    run.error = "Force released by admin"
    run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, run, commit=False, terminate_grid_sessions=True)
    queue_event_for_session(
        db,
        "run.cancelled",
        {
            "run_id": str(run.id),
            "name": run.name,
            "cancelled_by": "admin (force release)",
        },
        severity="warning",
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def expire_run(db: AsyncSession, run: TestRun, reason: str, *, publisher: EventBus | None = None) -> None:
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

    await _clear_desired_grid_run_id_for_run(db, run=locked_run, caller="run_expire", reason=effective_reason)
    locked_run.state = RunState.expired
    locked_run.error = effective_reason
    locked_run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, locked_run, commit=False, terminate_grid_sessions=True)

    if expired_from_preparing:
        queue_event_for_session(
            db,
            "run.never_activated",
            {
                "run_id": str(locked_run.id),
                "name": locked_run.name,
                "reason": effective_reason,
            },
            severity="warning",
            publisher=publisher or _default_event_bus,
        )

    queue_event_for_session(
        db,
        "run.expired",
        {
            "run_id": str(locked_run.id),
            "name": locked_run.name,
            "reason": effective_reason,
        },
        severity="critical",
        publisher=publisher or _default_event_bus,
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
