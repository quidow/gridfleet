from __future__ import annotations

import asyncio
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.orm import joinedload, selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.leader.advisory import LeadershipLost, assert_current_leader
from app.core.observability import get_logger, observe_background_loop
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services import intent as intent_service
from app.grid import appium_direct
from app.grid.allocation import node_target, resolve_router_target
from app.lifecycle.services import policy as lifecycle_policy
from app.sessions import probe_inflight
from app.sessions import service as session_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

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

GRID_NODE_STOPPED_SESSIONS_CLOSED_TOTAL = Counter(
    "gridfleet_grid_node_stopped_sessions_closed",
    "Running sessions closed by the observation sweep because their device's node "
    "has desired_state != running (operator stopped the node out from under the session).",
)

# Liveness sweep per-session verdict. Separating these out (Q13) replaces the prior
# overloaded ``bool | None`` probe return (where ``True`` meant both "alive" and
# "nothing to probe") and lets ``reap_reason`` be computed exactly once per session.
_LivenessAction = Literal["leave", "leave_indeterminate", "defer", "close_reap", "close_node_stopped", "close_dead"]


@dataclass(frozen=True, slots=True)
class _LivenessVerdict:
    action: _LivenessAction
    reap_reason: str | None
    defer_detail: str | None = None


# Module-level wake hook (P2). The session_sync loop registers its running service's
# ``wake`` here on startup; other leader-owned loops (e.g. the allocation reaper, which
# runs in the same process as the leader's session_sync loop) call
# ``request_session_sync_wake`` to ring the doorbell after they free a device, so the
# sweep runs immediately instead of waiting up to one poll interval. In-process only:
# the reaper and the leader session_sync loop are co-located on the leader, so a direct
# in-memory hook is sufficient and avoids a bus round trip.
_WAKE_HOOK: Callable[[], None] | None = None


def register_session_sync_wake_hook(hook: Callable[[], None]) -> None:
    global _WAKE_HOOK
    _WAKE_HOOK = hook


def request_session_sync_wake() -> None:
    """Ring the session_sync doorbell if a loop is registered; no-op otherwise."""
    if _WAKE_HOOK is not None:
        _WAKE_HOOK()


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
        """Close DB-truth running sessions that should no longer pin their device.

        A running session is closed when one of these is unambiguously true:

        1. Liveness — Appium reports the session definitively gone. An
           indeterminate network verdict is left untouched; we never kill on
           uncertainty.
        2. Node stopped — the device's Appium node has ``desired_state ==
           stopped`` (an operator stopped the node out from under the live
           session). Operator intent is unambiguous, so close the session even
           though the probe is necessarily indeterminate (connection refused on
           a stopped process maps to ``None``) (C2). An ``observed-down`` node
           whose ``desired_state`` is still ``running`` (a crash with a respawn
           possibly in flight) is left to the probe.
        3. Idle / never-commanded — two cutoffs apply depending on whether the
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

        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(PROBE_CONCURRENCY_PER_HOST)
        )
        sessions_with_device = [s for s in running_sessions if s.device is not None]

        def _reap_reason(session: Session) -> str | None:
            """Return why a session should be reaped (idle / never-commanded), else None.

            NULL activity means the client never issued a command — age it against
            the first-command grace from ``started_at`` (the claim time). Any
            observed activity ages against the idle timeout; the ``started_at``
            fallback is intentionally gone (grace owns the never-commanded case).
            """
            if session.last_activity_at is None:
                return "never_commanded" if session.started_at < grace_cutoff else None
            return "idle" if session.last_activity_at < idle_cutoff else None

        def _node_stopped(session: Session) -> bool:
            node = session.device.appium_node if session.device is not None else None
            return node is not None and node.desired_state is not AppiumDesiredState.running

        # Probe phase: gather one verdict per session concurrently, bounded by a per-host
        # semaphore, so a single hung node cannot stall the sweep wall time (#10). The
        # reap_reason and node-stopped facts are computed once here (Q13) and carried into
        # the write loop so they are not recomputed. No DB access happens inside the gather.
        async def _probe(session: Session) -> _LivenessVerdict:
            device = session.device
            assert device is not None  # filtered above
            # Resolve via resolve_router_target — the same fallback every other consumer
            # uses (/routes, resume_claimed, run-release): prefer the live node_target but
            # fall back to Session.router_target stored at allocation when the live target
            # is unresolvable (node row gone / host association lost). The reap previously
            # used node_target directly, the one consumer that did not adopt the fallback.
            target = resolve_router_target(session)
            reap_reason = _reap_reason(session)
            node_stopped = _node_stopped(session)
            async with host_semaphores[device.host_id]:
                if reap_reason is not None or node_stopped:
                    if target is None:
                        # No resolvable Appium target at all: closing the DB row would
                        # orphan a possibly-still-live Appium session (and _kill_orphans
                        # also skips a None-target device), re-allocating the device while
                        # the session keeps holding it. Defer to a later tick when a target
                        # resolves rather than close blind (C3).
                        return _LivenessVerdict(
                            action="defer", reap_reason=reap_reason, defer_detail="no resolvable Appium target"
                        )
                    if not await appium_direct.terminate_session(target, session.session_id):
                        # Terminate unconfirmed (5xx/timeout): the Appium session may
                        # still be alive. Closing the row would free the device for
                        # re-allocation under the live foreign session (wave-5 #3) —
                        # keep the row and retry next tick, mirroring run-release.
                        # An already-gone session converges: terminate_session maps
                        # 404 to True, so the retry closes it.
                        return _LivenessVerdict(
                            action="defer", reap_reason=reap_reason, defer_detail="Appium terminate failed"
                        )
                    action: _LivenessAction = "close_reap" if reap_reason is not None else "close_node_stopped"
                    return _LivenessVerdict(action=action, reap_reason=reap_reason)
                if target is None:
                    return _LivenessVerdict(action="leave", reap_reason=None)  # nothing to probe
                alive = await appium_direct.session_alive(target, session.session_id)
                if alive is None:
                    return _LivenessVerdict(action="leave_indeterminate", reap_reason=None)
                return _LivenessVerdict(action="leave" if alive else "close_dead", reap_reason=None)

        verdicts = await asyncio.gather(*[_probe(s) for s in sessions_with_device])

        # Re-fence after the slow probe phase (node_health pattern): another backend
        # may have taken leadership while we awaited Appium — drop the write phase.
        await assert_current_leader(db, settings=self._settings)

        device_ids_to_restore: set[uuid.UUID] = set()
        for session, verdict in zip(sessions_with_device, verdicts, strict=True):
            device = session.device
            assert device is not None
            if verdict.action == "defer":
                logger.warning(
                    "grid_session_reap_deferred session=%s device=%s reason=%s (%s)",
                    session.session_id,
                    device.id,
                    verdict.reap_reason or "node_stopped",
                    verdict.defer_detail,
                )
                continue
            if verdict.action == "leave":
                continue
            if verdict.action == "leave_indeterminate":
                logger.debug("session_liveness_indeterminate session=%s device=%s", session.session_id, device.id)
                continue
            # Every remaining action closes the DB row the same way a vanished session is
            # closed, so the session stops pinning the device busy.
            if verdict.action == "close_reap":
                if verdict.reap_reason == "never_commanded":
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
            elif verdict.action == "close_node_stopped":
                GRID_NODE_STOPPED_SESSIONS_CLOSED_TOTAL.inc()
                logger.warning(
                    "grid_node_stopped_session_closed session=%s device=%s",
                    session.session_id,
                    device.id,
                )
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
        # Filter to desired-running nodes in SQL (#20) instead of loading every
        # node-bearing device and discarding the stopped ones in Python.
        device_stmt = (
            select(Device)
            .options(selectinload(Device.appium_node), selectinload(Device.host))
            .join(Device.appium_node)
            .where(AppiumNode.desired_state == AppiumDesiredState.running)
            .order_by(Device.id)
        )
        devices = (await db.execute(device_stmt)).scalars().all()

        # Resolve the routing target per device, then batched IN-queries over that
        # candidate set (#12): devices holding a pending row, known live ids per
        # device, and doomed terminal ids. The allocate->confirm window holds a
        # placeholder session_id while the real Appium id is created, so an
        # in-creation session is indistinguishable BY ID from a fresh foreign orphan.
        # On a device with a pending row the sweep therefore kills only ids it can
        # prove doomed — ids recorded on a TERMINAL row (the 409-confirm path stamps
        # the real id when the router's rollback may have failed; see
        # ``record_doomed_appium_session``) — and spares every unknown id, which may
        # be the pending allocation's own session, regardless of the row's age: the
        # allocation reaper owns expiring stale pending rows (claim window + confirm
        # grace).
        #
        # Residual trade-off (#7): an orphan whose id was never reported backend-side
        # (router died before confirm) is spared while ANY pending row lives. It
        # persists at most claim_window + confirm_grace, until the reaper fails the
        # pending row and frees the device; the next sweep tick then terminates it.
        candidates: list[tuple[Device, str]] = []
        for device in devices:
            target = node_target(device)
            if target is None:
                continue
            candidates.append((device, target))

        candidate_ids = [device.id for device, _ in candidates]
        devices_with_pending: set[uuid.UUID] = set()
        if candidate_ids:
            pending_rows = await db.execute(
                select(Session.device_id).where(
                    Session.device_id.in_(candidate_ids),
                    Session.status == SessionStatus.pending,
                    Session.ended_at.is_(None),
                )
            )
            devices_with_pending = {device_id for (device_id,) in pending_rows.all() if device_id is not None}

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
        known_ids_by_device: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
        if candidate_ids:
            known_rows = await db.execute(
                select(Session.device_id, Session.session_id).where(
                    Session.device_id.in_(candidate_ids),
                    live_session_predicate(),
                )
            )
            for device_id, session_id in known_rows.all():
                if device_id is not None:
                    known_ids_by_device[device_id].add(session_id)

        # Doomed ids for pending devices: a live id matching a TERMINAL row is provably
        # not the in-creation session (which has no row until confirm) and is killable
        # even while the pending allocation is in flight. Bounded by the enumerated live
        # ids, so the query never scans full session history.
        pending_live_ids = {
            live_id
            for (device, _), live_ids in zip(candidates, live_id_lists, strict=True)
            if device.id in devices_with_pending
            for live_id in (live_ids or [])
        }
        doomed_ids_by_device: defaultdict[uuid.UUID, set[str]] = defaultdict(set)
        if pending_live_ids:
            doomed_rows = await db.execute(
                select(Session.device_id, Session.session_id).where(
                    Session.device_id.in_([d for d in candidate_ids if d in devices_with_pending]),
                    Session.session_id.in_(pending_live_ids),
                    Session.ended_at.is_not(None),
                )
            )
            for device_id, session_id in doomed_rows.all():
                if device_id is not None:
                    doomed_ids_by_device[device_id].add(session_id)

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
                if device.id in devices_with_pending and live_id not in doomed_ids_by_device[device.id]:
                    # Unknown id on a pending device — may be the pending allocation's
                    # own in-creation session; spare it (see the candidate comment).
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

        Wakes on either the doorbell (rung via ``request_session_sync_wake`` —
        e.g. the allocation reaper after it frees a reaped pending device, P2)
        or the registry-configured timeout, whichever comes first. The poll runs
        as a drift reconciler.
        """
        sync = self._services.sync
        # Register this running service's doorbell so co-located leader loops can ring it
        # (P2). Registered on the leader process; non-leader loops exit before reaching
        # work, so the last registration wins and points at the active loop.
        register_session_sync_wake_hook(sync.wake)
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
