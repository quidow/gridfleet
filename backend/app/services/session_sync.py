import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.database import async_session
from app.models.device import Device, DeviceOperationalState
from app.models.session import Session, SessionStatus
from app.models.test_run import TERMINAL_STATES, RunState
from app.observability import get_logger, observe_background_loop
from app.services import device_locking, grid_service, lifecycle_policy, run_service, session_service
from app.services.device_state import ready_operational_state, set_operational_state
from app.services.session_viability import PROBE_TEST_NAME
from app.services.settings_service import settings_service

logger = get_logger(__name__)
LOOP_NAME = "session_sync"
RESERVED_SESSION_ID = "reserved"


def _extract_sessions_from_grid(grid_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract active sessions from Grid /status response.

    Returns {session_id: {connection_target, test_name, capabilities}} mapping.
    Grid 4 stores sessions under node.slots[].session (not node.sessions).

    Probe sessions (set by session_viability) are filtered out so the sync
    loop does not persist transient probe activity as real sessions.
    """
    sessions: dict[str, dict[str, Any]] = {}
    value = grid_data.get("value", {})
    if not isinstance(value, dict):
        return sessions

    for node in value.get("nodes", []):
        for slot in node.get("slots", []):
            sess = slot.get("session")
            if not sess:
                continue
            sid = sess.get("sessionId")
            if not sid or sid == RESERVED_SESSION_ID:
                continue
            caps = sess.get("capabilities", {})
            if isinstance(caps, dict):
                if caps.get("gridfleet:probeSession") is True:
                    continue
                if caps.get("gridfleet:testName") == PROBE_TEST_NAME:
                    continue
            connection_target = (
                (caps.get("appium:udid") or caps.get("appium:deviceName")) if isinstance(caps, dict) else None
            )
            device_id = (
                (caps.get("gridfleet:deviceId") or caps.get("appium:gridfleet:deviceId"))
                if isinstance(caps, dict)
                else None
            )
            test_name = caps.get("gridfleet:testName") if isinstance(caps, dict) else None
            sessions[sid] = {
                "connection_target": connection_target,
                "device_id": device_id,
                "test_name": test_name,
                "requested_capabilities": caps if isinstance(caps, dict) else None,
            }

    return sessions


async def _sweep_stale_stop_pending(db: AsyncSession) -> None:
    """Backstop sweep: clear stop_pending on devices that have no running sessions.

    Protects against any session-end path that bypassed
    `lifecycle_policy.complete_deferred_stop_if_session_ended`. Runs every session_sync
    cycle (independent of Grid availability) and is a no-op for devices that
    are correctly clean.

    Selects only ``Device.id`` ordered for deterministic iteration; the row
    lock is taken inside ``handle_session_finished`` per device, never as a
    batch.
    """
    stmt = select(Device.id).where(Device.lifecycle_policy_state["stop_pending"].astext == "true").order_by(Device.id)
    result = await db.execute(stmt)
    device_ids = list(result.scalars().all())
    for device_id in device_ids:
        device = await db.get(Device, device_id)
        if device is None:
            continue
        await lifecycle_policy.complete_deferred_stop_if_session_ended(db, device)


async def _sync_sessions(db: AsyncSession) -> None:
    """Sync Grid sessions with the Session table."""
    grid_data = await grid_service.get_grid_status()

    # Skip the Grid-driven sync when the hub is unreachable, but still run
    # the stale ``stop_pending`` sweep — the sweep relies on DB state only,
    # so it must heal historical rows even during Grid outages.
    if not grid_data.get("value", {}).get("ready", False) and "error" in grid_data:
        logger.debug("Grid unreachable, skipping Grid session sync (sweep still runs)")
        await _sweep_stale_stop_pending(db)
        await db.commit()
        return

    active = _extract_sessions_from_grid(grid_data)
    running_stmt = select(Session).where(
        Session.status == SessionStatus.running,
        Session.ended_at.is_(None),
    )
    running_result = await db.execute(running_stmt)
    known_running = {session.session_id: str(session.device_id) for session in running_result.scalars().all()}
    known_session_ids: set[str] = set()
    if active:
        known_result = await db.execute(select(Session.session_id).where(Session.session_id.in_(active)))
        known_session_ids = set(known_result.scalars().all())

    # Process new sessions
    for sid, info in active.items():
        if sid in known_running or sid in known_session_ids:
            continue

        connection_target = info.get("connection_target")
        if not connection_target:
            continue

        device_id = info.get("device_id")
        if isinstance(device_id, str) and device_id:
            try:
                stmt = select(Device).where(Device.id == uuid.UUID(device_id))
            except ValueError:
                stmt = select(Device).where(Device.connection_target == connection_target)
        else:
            stmt = select(Device).where(Device.connection_target == connection_target)
        result = await db.execute(stmt)
        device = result.scalar_one_or_none()

        if device is None:
            logger.warning("Grid session %s references unknown connection target: %s", sid, connection_target)
            continue

        # Resolve reservation before creating session so run_id is persisted
        reservation_run, reservation_entry = await run_service.get_device_reservation_with_entry(db, device.id)
        reservation_run_id = (
            reservation_run.id
            if reservation_run is not None and not run_service.reservation_entry_is_excluded(reservation_entry)
            else None
        )

        # Create session record
        session = Session(
            session_id=sid,
            device_id=device.id,
            test_name=info.get("test_name"),
            status=SessionStatus.running,
            requested_capabilities=info.get("requested_capabilities"),
            run_id=reservation_run_id,
        )
        db.add(session)

        # Mark device busy under row lock
        locked_device = await device_locking.lock_device(db, device.id)
        await set_operational_state(locked_device, DeviceOperationalState.busy, publish_event=False)
        activated_run = await run_service.signal_active_for_device_session_no_commit(db, device.id)
        session_service.queue_session_started_event(
            db,
            session,
            device=device,
            run_id=str(activated_run.id) if activated_run and activated_run.state == RunState.active else None,
        )
        logger.info("Tracked new session %s on device %s (%s)", sid, device.name, connection_target)

    # Process ended sessions
    ended_sids = [sid for sid in known_running if sid not in active]
    for sid in ended_sids:
        device_id_str = known_running[sid]

        sess_stmt = (
            select(Session)
            .options(selectinload(Session.device), joinedload(Session.run))
            .where(Session.session_id == sid, Session.status == SessionStatus.running)
        )
        sess_result = await db.execute(sess_stmt)
        ended_session = sess_result.scalar_one_or_none()

        if ended_session:
            ended_device = ended_session.device
            ended_session.ended_at = datetime.now(UTC)
            attached_run = ended_session.run
            if attached_run is not None and attached_run.state in TERMINAL_STATES - {RunState.completed}:
                ended_session.status = SessionStatus.error
                ended_session.error_type = "run_released"
                ended_session.error_message = f"Run ended while session was still running ({attached_run.state.value})"
            else:
                ended_session.status = SessionStatus.passed  # default; pytest helper can override
            session_service.queue_session_ended_event(db, ended_session, device=ended_device)
            logger.info("Session %s ended", sid)

        # Check if device has other running sessions
        count_stmt = select(Session).where(
            Session.device_id == device_id_str,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
        count_result = await db.execute(count_stmt)
        still_running = count_result.scalars().first() is not None
        if not still_running:
            dev_stmt = select(Device).where(Device.id == device_id_str)
            dev_result = await db.execute(dev_stmt)
            device = dev_result.scalar_one_or_none()
            if device is not None:
                outcome = await lifecycle_policy.handle_session_finished(db, device)
                if outcome is lifecycle_policy.DeferredStopOutcome.AUTO_STOPPED:
                    continue
                if outcome is lifecycle_policy.DeferredStopOutcome.RUNNING_SESSION_EXISTS:
                    # A fresh client session arrived between our running-set
                    # check and the locked check inside the helper; leave the
                    # device busy so the new session keeps it.
                    continue
            if device and device.operational_state == DeviceOperationalState.busy:
                locked_device = await device_locking.lock_device(db, device.id)
                if locked_device.operational_state == DeviceOperationalState.busy:
                    # Authoritative recheck under the row lock. ``handle_session_finished``
                    # only does the locked running-session check when ``stop_pending``
                    # is set; in the common no-deferred-stop path it returns NO_PENDING
                    # without ever consulting the Session table under lock, so a fresh
                    # session inserted between the outer ``still_running`` check and
                    # this restore could be skipped past. Re-check here so we never
                    # restore a device that now hosts a new running session.
                    fresh_running_stmt = select(Session.id).where(
                        Session.device_id == locked_device.id,
                        Session.status == SessionStatus.running,
                        Session.ended_at.is_(None),
                    )
                    fresh_running = (await db.execute(fresh_running_stmt)).scalar_one_or_none()
                    if fresh_running is None:
                        await set_operational_state(
                            locked_device,
                            await ready_operational_state(db, locked_device),
                            reason="Session ended",
                        )

    await _sweep_stale_stop_pending(db)
    await db.commit()


async def session_sync_loop() -> None:
    """Background loop that syncs Grid sessions."""
    while True:
        interval = float(settings_service.get("grid.session_poll_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _sync_sessions(db)
        except Exception:
            logger.exception("Session sync failed")
        await asyncio.sleep(interval)
