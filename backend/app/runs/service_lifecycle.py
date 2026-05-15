import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.devices import schemas as device_schemas
from app.devices.models import DeviceReservation
from app.devices.services import (
    capability,
    intent_types,
    lifecycle_incidents,
    maintenance,
    platform_label,
    readiness,
    state,
)
from app.devices.services import (
    intent as intent_service,
)
from app.events import queue_event_for_session
from app.packs.services import platform_resolver as pack_platform_resolver
from app.runs.models import TERMINAL_STATES, RunState, TestRun
from app.runs.service_lifecycle_release import (
    _clear_desired_grid_run_id_for_run,
    _complete_deferred_stops_post_commit,
    _release_devices,
)
from app.runs.service_query import get_run
from app.runs.service_reservation_lookup import (
    get_device_reservation_with_entry,
    reservation_entry_is_excluded,
)

assert_runnable = pack_platform_resolver.assert_runnable
GRID_ROUTING = intent_types.GRID_ROUTING
NODE_PROCESS = intent_types.NODE_PROCESS
PRIORITY_COOLDOWN = intent_types.PRIORITY_COOLDOWN
PRIORITY_FORCED_RELEASE = intent_types.PRIORITY_FORCED_RELEASE
PRIORITY_RUN_ROUTING = intent_types.PRIORITY_RUN_ROUTING
RECOVERY = intent_types.RECOVERY
RESERVATION = intent_types.RESERVATION
IntentRegistration = intent_types.IntentRegistration
DeviceLifecyclePolicySummaryState = device_schemas.DeviceLifecyclePolicySummaryState
is_ready_for_use_async = readiness.is_ready_for_use_async
ready_operational_state = state.ready_operational_state
set_hold = state.set_hold
set_operational_state = state.set_operational_state
capability_service = capability
register_intents_and_reconcile = intent_service.register_intents_and_reconcile
revoke_intents_and_reconcile = intent_service.revoke_intents_and_reconcile
lifecycle_incident_service = lifecycle_incidents
maintenance_service = maintenance
platform_label_service = platform_label

logger = logging.getLogger(__name__)


async def _get_run_for_update(db: AsyncSession, run_id: uuid.UUID) -> TestRun | None:
    stmt = (
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def signal_ready(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
    run = await _get_run_for_update(db, run_id)
    if run is None:
        raise ValueError("Run not found")
    if run.state != RunState.preparing:
        raise ValueError(f"Cannot signal ready from state '{run.state.value}', expected 'preparing'")

    now = datetime.now(UTC)
    run.state = RunState.active
    run.started_at = now
    run.last_heartbeat = now
    queue_event_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def signal_active(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
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
    queue_event_for_session(db, "run.active", {"run_id": str(run.id), "name": run.name})
    await db.commit()
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def signal_active_for_device_session(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    run = await signal_active_for_device_session_no_commit(db, device_id)
    if run is None:
        return None
    await db.commit()
    refreshed_run = await get_run(db, run.id)
    assert refreshed_run is not None
    return refreshed_run


async def signal_active_for_device_session_no_commit(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or reservation_entry_is_excluded(entry):
        return None
    locked_run = await _get_run_for_update(db, run.id)
    if locked_run is None:
        return None
    if locked_run.state == RunState.active:
        if locked_run.started_at is None:
            now = datetime.now(UTC)
            locked_run.started_at = now
            locked_run.last_heartbeat = locked_run.last_heartbeat or now
        return locked_run
    if locked_run.state != RunState.preparing:
        return None
    now = datetime.now(UTC)
    locked_run.state = RunState.active
    locked_run.started_at = now
    locked_run.last_heartbeat = now
    queue_event_for_session(db, "run.active", {"run_id": str(locked_run.id), "name": locked_run.name})
    return locked_run


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


async def complete_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
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
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def cancel_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
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
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def force_release(db: AsyncSession, run_id: uuid.UUID) -> TestRun:
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
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
    run = await get_run(db, run_id)
    assert run is not None
    return run


async def expire_run(db: AsyncSession, run: TestRun, reason: str) -> None:
    """Expire a run due to heartbeat or TTL timeout. Called by the reaper."""

    locked_run = await _get_run_for_update(db, run.id)
    if locked_run is None:
        return
    if locked_run.state in TERMINAL_STATES:
        await db.commit()
        return

    await _clear_desired_grid_run_id_for_run(db, run=locked_run, caller="run_expire", reason=reason)
    locked_run.state = RunState.expired
    locked_run.error = reason
    locked_run.completed_at = datetime.now(UTC)
    cleanup_ids = await _release_devices(db, locked_run, commit=False, terminate_grid_sessions=True)

    queue_event_for_session(
        db,
        "run.expired",
        {
            "run_id": str(locked_run.id),
            "name": locked_run.name,
            "reason": reason,
        },
    )
    await db.commit()
    await _complete_deferred_stops_post_commit(db, cleanup_ids)
