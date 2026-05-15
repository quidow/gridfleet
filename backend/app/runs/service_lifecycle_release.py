import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices import locking as device_locking
from app.devices import schemas as device_schemas
from app.devices.models import Device, DeviceHold, DeviceOperationalState
from app.devices.services import (
    capability,
    intent_types,
    lifecycle_incidents,
    lifecycle_policy,
    maintenance,
    platform_label,
    readiness,
    state,
)
from app.devices.services import (
    intent as intent_service,
)
from app.grid import service as grid_service
from app.packs.services import platform_resolver as pack_platform_resolver
from app.runs.models import TestRun
from app.sessions.models import Session, SessionStatus

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


async def _mark_running_sessions_released(
    db: AsyncSession,
    run: TestRun,
    released_at: datetime,
    *,
    terminate_grid_sessions: bool,
) -> None:
    if not terminate_grid_sessions:
        # complete_run path: session lifecycle is owned by the testkit/operator.
        # Leaving running rows untouched keeps _device_has_running_session honest
        # so devices with live Grid sessions are not freed under the run.
        return

    stmt = select(Session).where(
        Session.run_id == run.id,
        Session.status == SessionStatus.running,
        Session.ended_at.is_(None),
    )
    result = await db.execute(stmt)
    sessions = result.scalars().all()
    if not sessions:
        return

    error_message = run.error if run.error else f"Run ended while session was still running ({run.state.value})"
    for session in sessions:
        if not await grid_service.terminate_grid_session(session.session_id):
            logger.warning(
                "Leaving session %s running because Grid deletion failed during run %s release",
                session.session_id,
                run.id,
            )
            continue

        session.status = SessionStatus.error
        session.ended_at = released_at
        session.error_type = "run_released"
        session.error_message = error_message


async def _device_has_running_session(db: AsyncSession, device_id: uuid.UUID) -> bool:
    stmt = (
        select(Session.id)
        .where(
            Session.device_id == device_id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _clear_desired_grid_run_id_for_run(
    db: AsyncSession,
    *,
    run: TestRun,
    caller: str,
    actor: str | None = None,
    reason: str | None = None,
) -> None:
    del actor
    for reservation in run.device_reservations:
        if reservation.released_at is not None:
            continue
        try:
            device = await device_locking.lock_device(db, reservation.device_id, load_sessions=False)
        except NoResultFound:
            continue
        sources = [
            f"run:{run.id}",
            f"cooldown:node:{run.id}",
            f"cooldown:grid:{run.id}",
            f"cooldown:reservation:{run.id}",
            f"cooldown:recovery:{run.id}",
        ]
        if caller == "run_force_release":
            await register_intents_and_reconcile(
                db,
                device_id=device.id,
                intents=[
                    IntentRegistration(
                        source=f"forced_release:{run.id}",
                        axis=NODE_PROCESS,
                        run_id=run.id,
                        payload={"action": "stop", "priority": PRIORITY_FORCED_RELEASE, "stop_mode": "hard"},
                    )
                ],
                reason=reason or f"force release run {run.id}",
            )
        await revoke_intents_and_reconcile(
            db,
            device_id=device.id,
            sources=sources,
            reason=reason or f"clear run {run.id} intents",
        )


async def _release_devices(
    db: AsyncSession,
    run: TestRun,
    *,
    commit: bool = True,
    terminate_grid_sessions: bool = False,
) -> list[uuid.UUID]:
    """Release all active reservations for this run and restore device statuses.

    Returns the device IDs that need a follow-up
    ``complete_deferred_stop_if_session_ended`` pass. The caller MUST run
    ``_complete_deferred_stops_post_commit`` after the encompassing run-state
    commit; the lifecycle helper commits internally (via
    ``handle_node_crash``) and must not be invoked while the run-state
    transaction is still open, otherwise a partial commit can land on disk if
    a later step in the same call raises.
    """

    active_reservations = [reservation for reservation in run.device_reservations if reservation.released_at is None]
    released_at = datetime.now(UTC)
    await _mark_running_sessions_released(
        db,
        run,
        released_at,
        terminate_grid_sessions=terminate_grid_sessions,
    )

    if not active_reservations:
        if commit:
            await db.commit()
        return []

    device_ids = sorted({reservation.device_id for reservation in active_reservations})
    locked_devices = {device.id: device for device in await device_locking.lock_devices(db, device_ids)}
    devices_pending_lifecycle_cleanup: list[uuid.UUID] = []

    for reservation in active_reservations:
        reservation.released_at = released_at
        device = locked_devices.get(reservation.device_id)
        if device is None:
            logger.warning(
                "Reservation %s references missing device %s; skipping availability restore",
                reservation.id,
                reservation.device_id,
            )
            continue
        if device.hold == DeviceHold.maintenance:
            devices_pending_lifecycle_cleanup.append(device.id)
            continue
        if device.hold != DeviceHold.reserved and device.operational_state != DeviceOperationalState.busy:
            devices_pending_lifecycle_cleanup.append(device.id)
            continue
        if device.hold == DeviceHold.reserved:
            await set_hold(device, None, reason=f"Run '{run.name}' ended ({run.state.value})")
        if device.operational_state == DeviceOperationalState.busy and await _device_has_running_session(db, device.id):
            devices_pending_lifecycle_cleanup.append(device.id)
            continue
        await set_operational_state(
            device,
            await ready_operational_state(db, device),
            reason=f"Run '{run.name}' ended ({run.state.value})",
        )
        devices_pending_lifecycle_cleanup.append(device.id)
    if commit:
        await db.commit()
    return devices_pending_lifecycle_cleanup


async def _complete_deferred_stops_post_commit(db: AsyncSession, device_ids: list[uuid.UUID]) -> None:
    """Run ``complete_deferred_stop_if_session_ended`` for each device after
    the caller's run-state commit landed. Skips devices that vanished in the
    meantime."""
    for device_id in device_ids:
        device = await db.get(Device, device_id)
        if device is None:
            continue
        await lifecycle_policy.complete_deferred_stop_if_session_ended(db, device)
