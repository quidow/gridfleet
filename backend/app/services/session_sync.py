import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.device import Device, DeviceAvailabilityStatus
from app.models.session import Session, SessionStatus
from app.models.test_run import RunState
from app.observability import get_logger, observe_background_loop
from app.services import grid_service, lifecycle_policy, run_service, session_service
from app.services.device_availability import restore_post_busy_availability_status, set_device_availability_status
from app.services.settings_service import settings_service

logger = get_logger(__name__)
LOOP_NAME = "session_sync"
RESERVED_SESSION_ID = "reserved"


def _extract_sessions_from_grid(grid_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract active sessions from Grid /status response.

    Returns {session_id: {connection_target, test_name, capabilities}} mapping.
    Grid 4 stores sessions under node.slots[].session (not node.sessions).
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
            connection_target = caps.get("appium:udid") or caps.get("appium:deviceName")
            device_id = caps.get("gridfleet:deviceId") or caps.get("appium:gridfleet:deviceId")
            test_name = caps.get("gridfleet:testName")
            sessions[sid] = {
                "connection_target": connection_target,
                "device_id": device_id,
                "test_name": test_name,
                "requested_capabilities": caps if isinstance(caps, dict) else None,
            }

    return sessions


async def _sync_sessions(db: AsyncSession) -> None:
    """Sync Grid sessions with the Session table."""
    grid_data = await grid_service.get_grid_status()

    # Skip if grid is unreachable
    if not grid_data.get("value", {}).get("ready", False) and "error" in grid_data:
        logger.debug("Grid unreachable, skipping session sync")
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

        # Mark device busy
        await set_device_availability_status(device, DeviceAvailabilityStatus.busy, publish_event=False)
        activated_run = await run_service.signal_active_for_device_session(db, device.id)
        await session_service.publish_session_started_event(
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
            .options(selectinload(Session.device))
            .where(Session.session_id == sid, Session.status == SessionStatus.running)
        )
        sess_result = await db.execute(sess_stmt)
        ended_session = sess_result.scalar_one_or_none()

        if ended_session:
            ended_device = ended_session.device
            ended_session.ended_at = datetime.now(UTC)
            ended_session.status = SessionStatus.passed  # default; pytest helper can override
            await session_service.publish_session_ended_event(ended_session, device=ended_device)
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
            if device is not None and await lifecycle_policy.handle_session_finished(db, device):
                continue
            if device and device.availability_status == DeviceAvailabilityStatus.busy:
                await restore_post_busy_availability_status(db, device)

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
