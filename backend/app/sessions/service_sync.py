from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.appium_nodes.models import AppiumDesiredState
from app.core.leader.advisory import LeadershipLost, assert_current_leader
from app.core.observability import get_logger, observe_background_loop
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services import intent as intent_service
from app.grid import appium_direct
from app.grid.allocation import node_target
from app.lifecycle.services import policy as lifecycle_policy
from app.sessions import probe_inflight
from app.sessions import service as session_service
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.protocols import DeviceSessionLifecycle
    from app.sessions.services_container import SessionServices

logger = get_logger(__name__)
LOOP_NAME = "session_sync"

SESSION_SYNC_WAKE_SOURCE_TOTAL = Counter(
    "gridfleet_session_sync_wake_source",
    "Why session_sync_loop ran a cycle: doorbell (bus event) or tick (timeout).",
    labelnames=("source",),
)

GRID_ORPHAN_SESSIONS_KILLED_TOTAL = Counter(
    "gridfleet_grid_orphan_sessions_killed",
    "Appium sessions terminated by the observation sweep because no DB row tracks them.",
)


class SessionSyncService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        lifecycle: DeviceSessionLifecycle,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._lifecycle = lifecycle
        self._doorbell: asyncio.Event | None = None  # lazy: created on first access on the running loop

    def _get_doorbell(self) -> asyncio.Event:
        if self._doorbell is None:
            self._doorbell = asyncio.Event()
        return self._doorbell

    def wake(self) -> None:
        self._get_doorbell().set()

    async def wait_for_wake(self, timeout: float) -> bool:
        """Wait for a doorbell wake or timeout; clear and report which fired.
        Returns True if doorbell-woken, False on timeout."""
        doorbell = self._get_doorbell()
        try:
            await asyncio.wait_for(doorbell.wait(), timeout=timeout)
            woke = True
        except TimeoutError:
            woke = False
        doorbell.clear()
        return woke

    async def sync(self, db: AsyncSession) -> None:
        """Observation sweep: reconcile DB-truth sessions against live Appium nodes.

        The Selenium hub is gone. This loop polls each device's Appium server
        directly (``app.grid.appium_direct``):

        1. Liveness — every running DB session is probed; a definitively dead
           one is closed through the same ended path the allocator uses. An
           indeterminate (network) verdict is left untouched: we never kill on
           uncertainty.
        2. Orphan kill — each running node is enumerated; any Appium session
           with no matching DB row (and no in-flight viability probe) is
           terminated so a leaked session cannot pin a device busy forever.

        The loop never inserts or hydrates Session rows: row creation is owned
        by the allocation API. It also runs the stale ``stop_pending`` sweep,
        which depends on DB state only.
        """
        # Fence: a foreign leader must not write. The Appium probes below are
        # slow external calls, but the fence runs first so we drop the whole
        # cycle's writes if leadership changed before we started.
        await assert_current_leader(db, settings=self._settings)

        await self._check_liveness(db)
        await self._kill_orphans(db)
        await self._sweep_stale_stop_pending(db)
        await db.commit()

    async def _check_liveness(self, db: AsyncSession) -> None:
        """Close DB-truth running sessions that Appium reports as gone."""
        running_stmt = (
            select(Session)
            .options(
                selectinload(Session.device).selectinload(Device.appium_node),
                selectinload(Session.device).selectinload(Device.host),
                joinedload(Session.run),
            )
            .where(
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
            )
        )
        running_sessions = (await db.execute(running_stmt)).scalars().all()

        device_ids_to_restore: set[uuid.UUID] = set()
        for session in running_sessions:
            device = session.device
            if device is None:
                continue
            target = node_target(device)
            if target is None:
                continue
            alive = await appium_direct.session_alive(target, session.session_id)
            if alive is None:
                logger.debug("session_liveness_indeterminate session=%s device=%s", session.session_id, device.id)
                continue
            if alive:
                continue
            await self._end_session(db, session)
            if session.device_id is not None:
                device_ids_to_restore.add(session.device_id)

        for device_id in sorted(device_ids_to_restore):
            await self._restore_device_after_session_end(db, device_id)

    async def _end_session(self, db: AsyncSession, session: Session) -> None:
        """Close a single running session the same way the allocator does."""
        sid = session.session_id
        await session_service.close_running_session(db, session, attached_run=session.run, publisher=self._publisher)
        logger.info("Session %s ended", sid)

    async def _restore_device_after_session_end(self, db: AsyncSession, device_id: uuid.UUID) -> None:
        """Per-device still-running check + lifecycle handler + restore."""
        count_stmt = select(Session).where(
            Session.device_id == device_id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
        if (await db.execute(count_stmt)).scalars().first() is not None:
            return
        device = (await db.execute(select(Device).where(Device.id == device_id))).scalar_one_or_none()
        if device is None:
            return
        outcome = await self._lifecycle.handle_session_finished(db, device)
        if outcome is lifecycle_policy.DeferredStopOutcome.AUTO_STOPPED:
            return
        if outcome is lifecycle_policy.DeferredStopOutcome.RUNNING_SESSION_EXISTS:
            # A fresh client session arrived between our running-set check and
            # the locked check inside the helper; leave the device busy.
            return
        # Authoritative recheck under the row lock. ``handle_session_finished``
        # may have already derived the correct state; a fresh session inserted
        # between it and this lock must override that derivation. Always recheck.
        locked_device = await device_locking.lock_device(db, device.id)
        fresh_running_stmt = select(Session.id).where(
            Session.device_id == locked_device.id,
            Session.status == SessionStatus.running,
            Session.ended_at.is_(None),
        )
        fresh_running = (await db.execute(fresh_running_stmt)).first()
        reason = "Session ended" if fresh_running is None else "Fresh session started"
        # Mark dirty either way: the reconciler derives available/offline from
        # durable facts when no session remains, or restores busy when one does.
        await intent_service.IntentService(db).mark_dirty_and_reconcile(
            locked_device.id, reason=reason, publisher=self._publisher
        )

    async def _kill_orphans(self, db: AsyncSession) -> None:
        """Terminate Appium sessions with no tracking DB row, per running node."""
        claim_window = int(self._settings.get("grid.claim_window_sec"))
        device_stmt = (
            select(Device).options(selectinload(Device.appium_node), selectinload(Device.host)).join(Device.appium_node)
        )
        devices = (await db.execute(device_stmt)).scalars().all()
        for device in devices:
            node = device.appium_node
            if node is None or node.desired_state is not AppiumDesiredState.running:
                continue
            target = node_target(device)
            if target is None:
                continue
            # Allocate→confirm window: a pending row holds a placeholder session_id
            # while the real Appium id is being created. The live Appium session is
            # absent from known_ids (placeholders only), so the sweep would kill an
            # in-creation session. Skip the whole device while any in-window pending
            # row exists — the allocation reaper owns that window (an over-age pending
            # row is the reaper's to fail, so we let the sweep proceed once it expires).
            window_start = datetime.now(UTC) - timedelta(seconds=claim_window)
            pending_in_window_stmt = select(Session.id).where(
                Session.device_id == device.id,
                Session.status == SessionStatus.pending,
                Session.ended_at.is_(None),
                Session.started_at >= window_start,
            )
            if (await db.execute(pending_in_window_stmt)).first() is not None:
                continue
            live_ids = await appium_direct.list_sessions(target)
            if live_ids is None:
                logger.debug("session_discovery_unavailable device=%s target=%s", device.id, target)
                continue
            if not live_ids:
                continue
            known_stmt = select(Session.session_id).where(
                Session.device_id == device.id,
                Session.status.in_((SessionStatus.running, SessionStatus.pending)),
                Session.ended_at.is_(None),
            )
            known_ids = set((await db.execute(known_stmt)).scalars().all())
            if probe_inflight.is_probe_inflight(str(device.id)):
                continue
            for live_id in live_ids:
                if live_id in known_ids:
                    continue
                terminated = await appium_direct.terminate_session(target, live_id)
                if terminated:
                    GRID_ORPHAN_SESSIONS_KILLED_TOTAL.inc()
                logger.warning(
                    "grid_orphan_session_killed session=%s device=%s target=%s terminated=%s",
                    live_id,
                    device.id,
                    target,
                    terminated,
                )

    async def _sweep_stale_stop_pending(self, db: AsyncSession) -> None:
        """Backstop sweep: clear stop_pending on devices that have no running sessions.

        Protects against any session-end path that bypassed
        `lifecycle_policy.complete_deferred_stop_if_session_ended`. Runs every session_sync
        cycle and is a no-op for devices that are correctly clean.

        Selects only ``Device.id`` ordered for deterministic iteration; the row
        lock is taken inside ``handle_session_finished`` per device, never as a
        batch.
        """
        stmt = (
            select(Device.id).where(Device.lifecycle_policy_state["stop_pending"].astext == "true").order_by(Device.id)
        )
        result = await db.execute(stmt)
        device_ids = list(result.scalars().all())
        for device_id in device_ids:
            device = await db.get(Device, device_id)
            if device is None:
                continue
            await self._lifecycle.complete_deferred_stop_if_session_ended(db, device)


class SessionSyncLoop:
    def __init__(self, *, services: SessionServices) -> None:
        self._services = services

    async def run(self) -> None:
        """Background loop that runs the session observation sweep.

        Wakes on either the doorbell (``wake()`` — currently no production
        caller since the grid event-bus subscriber was removed; kept for a
        future direct-observation trigger) or the registry-configured
        timeout, whichever comes first. The poll runs as a drift reconciler.
        """
        sync = self._services.sync
        while True:
            interval = float(self._services.settings.get("grid.session_poll_interval_sec"))
            try:
                async with observe_background_loop(LOOP_NAME, interval).cycle(), self._services.session_factory() as db:
                    await sync.sync(db)
            except LeadershipLost as exc:
                logger.error(
                    "session_sync_loop_leadership_lost",
                    reason=str(exc),
                    action="exiting_process_to_prevent_split_brain",
                )
                os._exit(70)
            except Exception:
                logger.exception("Session sync failed")
            woke = await sync.wait_for_wake(interval)
            SESSION_SYNC_WAKE_SOURCE_TOTAL.labels(source="doorbell" if woke else "tick").inc()
