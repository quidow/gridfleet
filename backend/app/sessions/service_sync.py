from __future__ import annotations

import asyncio
import os
from collections import defaultdict
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
# Bound concurrent Appium probes per host so a single hung node cannot stall the
# whole sweep, while a host's parallel devices are probed together. Mirrors
# node_health's PROBE_CONCURRENCY_PER_HOST.
PROBE_CONCURRENCY_PER_HOST = 2

SESSION_SYNC_WAKE_SOURCE_TOTAL = Counter(
    "gridfleet_session_sync_wake_source",
    "Why session_sync_loop ran a cycle: doorbell (bus event) or tick (timeout).",
    labelnames=("source",),
)

GRID_ORPHAN_SESSIONS_KILLED_TOTAL = Counter(
    "gridfleet_grid_orphan_sessions_killed",
    "Appium sessions terminated by the observation sweep because no DB row tracks them.",
)

GRID_IDLE_SESSIONS_REAPED_TOTAL = Counter(
    "gridfleet_grid_idle_sessions_reaped",
    "Running sessions terminated by the observation sweep for exceeding grid.session_idle_timeout_sec.",
)

GRID_NEVER_COMMANDED_SESSIONS_REAPED_TOTAL = Counter(
    "gridfleet_grid_never_commanded_sessions_reaped",
    "Running sessions with NULL last_activity_at terminated by the observation sweep for exceeding "
    "grid.session_first_command_grace_sec (the client never issued a command).",
)

GRID_ORPHAN_ENUM_UNAVAILABLE_TOTAL = Counter(
    "gridfleet_grid_orphan_enum_unavailable",
    "Orphan-sweep session enumeration (list_sessions) returned unavailable/unreachable for a node.",
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
        """Close DB-truth running sessions that Appium reports as gone, plus idle reaping.

        Two reasons close a running session here:

        1. Liveness — Appium reports the session definitively gone (an
           indeterminate network verdict is left untouched; we never kill on
           uncertainty).
        2. Idle / never-commanded — two cutoffs apply depending on whether the
           client has ever issued a command:

           * ``last_activity_at`` non-NULL (the router flushes it every 10 s once
             traffic flows): idle when older than ``grid.session_idle_timeout_sec``.
           * ``last_activity_at`` NULL (the client never issued a command — an
             abandoned-client zombie that claimed the device but never routed any
             WebDriver traffic): never-commanded when ``started_at`` (the claim
             time) is older than ``grid.session_first_command_grace_sec``.

           Appium does not reliably enforce newCommandTimeout idle kills, so an
           abandoned client that crashed without a DELETE would otherwise pin its
           device busy forever.
        """
        idle_timeout = int(self._settings.get("grid.session_idle_timeout_sec"))
        idle_cutoff = datetime.now(UTC) - timedelta(seconds=idle_timeout)
        grace = int(self._settings.get("grid.session_first_command_grace_sec"))
        grace_cutoff = datetime.now(UTC) - timedelta(seconds=grace)

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
            .order_by(Session.id)
        )
        running_sessions = (await db.execute(running_stmt)).scalars().all()

        # Probe phase: gather the per-session Appium probes concurrently, bounded by a
        # per-host semaphore, so a single hung node cannot stall the sweep wall time
        # (#10). Idle sessions get a terminate probe; live sessions get an alive probe.
        # No DB access happens inside the gather — writes are applied serially below.
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(PROBE_CONCURRENCY_PER_HOST)
        )
        sessions_with_device = [s for s in running_sessions if s.device is not None]

        def _reap_reason(session: Session) -> str | None:
            """Return why a session should be reaped, or None to leave it alone.

            NULL activity means the client never issued a command — age it against
            the first-command grace from ``started_at`` (the claim time). Any
            observed activity ages against the idle timeout; the ``started_at``
            fallback is intentionally gone (grace owns the never-commanded case).
            """
            if session.last_activity_at is None:
                return "never_commanded" if session.started_at < grace_cutoff else None
            return "idle" if session.last_activity_at < idle_cutoff else None

        async def _probe(session: Session) -> bool | None:
            device = session.device
            assert device is not None  # filtered above
            target = node_target(device)
            is_idle = _reap_reason(session) is not None
            async with host_semaphores[device.host_id]:
                if is_idle:
                    if target is not None:
                        await appium_direct.terminate_session(target, session.session_id)
                    return None  # verdict unused for idle reaps
                if target is None:
                    return True  # nothing to probe; treated as "leave alone"
                return await appium_direct.session_alive(target, session.session_id)

        probe_results = await asyncio.gather(*[_probe(s) for s in sessions_with_device])

        # Re-fence after the slow probe phase (node_health pattern): another backend
        # may have taken leadership while we awaited Appium — drop the write phase.
        await assert_current_leader(db, settings=self._settings)

        device_ids_to_restore: set[uuid.UUID] = set()
        for session, alive in zip(sessions_with_device, probe_results, strict=True):
            device = session.device
            assert device is not None
            reap_reason = _reap_reason(session)
            if reap_reason is not None:
                # Reap: the Appium session (if any target) was already terminated in the
                # probe phase; close the DB row the same way a vanished session is closed
                # so the abandoned session stops pinning the device busy.
                if reap_reason == "never_commanded":
                    GRID_NEVER_COMMANDED_SESSIONS_REAPED_TOTAL.inc()
                    logger.warning(
                        "grid_never_commanded_session_reaped session=%s device=%s started_at=%s grace_sec=%s",
                        session.session_id,
                        device.id,
                        session.started_at.isoformat(),
                        grace,
                    )
                else:
                    GRID_IDLE_SESSIONS_REAPED_TOTAL.inc()
                    logger.warning(
                        "grid_idle_session_reaped session=%s device=%s last_activity=%s idle_timeout_sec=%s",
                        session.session_id,
                        device.id,
                        session.last_activity_at.isoformat() if session.last_activity_at else None,
                        idle_timeout,
                    )
                await self._end_session(db, session)
                if session.device_id is not None:
                    device_ids_to_restore.add(session.device_id)
                continue
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
        device_stmt = (
            select(Device)
            .options(selectinload(Device.appium_node), selectinload(Device.host))
            .join(Device.appium_node)
            .order_by(Device.id)
        )
        devices = (await db.execute(device_stmt)).scalars().all()

        # In-memory filter to routable running-node devices, then two batched IN-queries
        # over that candidate set (#12): one for devices holding a pending row, one for
        # known live ids per device. The allocate->confirm window holds a placeholder
        # session_id while the real Appium id is created; the live session is absent from
        # known_ids so the sweep would kill an in-creation session. Skip the whole device
        # while ANY pending row exists, regardless of age — the allocation reaper owns
        # expiring stale pending rows (claim window + confirm grace), so the sweep must
        # not race it by killing a still-confirming session on an over-age row.
        #
        # Accepted trade-off (#7): a router-crash orphan on a pending device — an Appium
        # session created before the router died but never confirmed — is NOT cleaned by
        # this sweep while the pending row lives. It persists at most claim_window +
        # confirm_grace, until the reaper fails the pending row and frees the device; the
        # next sweep tick (the device no longer skipped) then terminates the orphan.
        routable: list[tuple[Device, str]] = []
        for device in devices:
            node = device.appium_node
            if node is None or node.desired_state is not AppiumDesiredState.running:
                continue
            target = node_target(device)
            if target is None:
                continue
            routable.append((device, target))

        routable_ids = [device.id for device, _ in routable]
        devices_with_pending: set[uuid.UUID] = set()
        if routable_ids:
            pending_rows = await db.execute(
                select(Session.device_id).where(
                    Session.device_id.in_(routable_ids),
                    Session.status == SessionStatus.pending,
                    Session.ended_at.is_(None),
                )
            )
            devices_with_pending = {device_id for (device_id,) in pending_rows.all() if device_id is not None}

        candidates: list[tuple[Device, str]] = [
            (device, target) for device, target in routable if device.id not in devices_with_pending
        ]

        # Probe phase: enumerate every candidate node's live sessions concurrently,
        # bounded per host, so a hung node cannot stall the sweep wall time (#10). No
        # DB access inside the gather.
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(PROBE_CONCURRENCY_PER_HOST)
        )

        async def _enumerate(target: str, host_id: uuid.UUID) -> list[str] | None:
            async with host_semaphores[host_id]:
                return await appium_direct.list_sessions(target)

        live_id_lists = await asyncio.gather(*[_enumerate(target, device.host_id) for device, target in candidates])

        # Re-fence after the slow enumeration phase (node_health pattern) before the
        # terminate/write loop below.
        await assert_current_leader(db, settings=self._settings)

        # Resolve the known (running/pending) session ids for every candidate device in
        # one IN-query, grouped by device (#12), before the write loop.
        candidate_ids = [device.id for device, _ in candidates]
        known_ids_by_device: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
        if candidate_ids:
            known_rows = await db.execute(
                select(Session.device_id, Session.session_id).where(
                    Session.device_id.in_(candidate_ids),
                    Session.status.in_((SessionStatus.running, SessionStatus.pending)),
                    Session.ended_at.is_(None),
                )
            )
            for device_id, session_id in known_rows.all():
                if device_id is not None:
                    known_ids_by_device[device_id].add(session_id)

        # Write phase: terminate orphans serially on the session.
        for (device, target), live_ids in zip(candidates, live_id_lists, strict=True):
            if live_ids is None:
                GRID_ORPHAN_ENUM_UNAVAILABLE_TOTAL.inc()
                logger.warning("grid_orphan_enum_unavailable device=%s target=%s", device.id, target)
                continue
            if not live_ids:
                continue
            known_ids = known_ids_by_device[device.id]
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
