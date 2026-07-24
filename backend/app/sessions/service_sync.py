from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.concurrency import per_key_semaphores
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services import intent as intent_service
from app.grid import appium_direct
from app.grid.allocation import node_target, resolve_router_target
from app.lifecycle.services import policy as lifecycle_policy
from app.sessions import service as session_service
from app.sessions.live_session_predicate import live_session_predicate
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME

if TYPE_CHECKING:
    import uuid
    from collections.abc import Callable

    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.sessions.protocols import DeviceSessionLifecycle

logger = get_logger(__name__)
# Freshness gate for the liveness probe (wave-5 #19): the router flushes
# last_activity_at every ~10s while traffic flows (router tasks.rs,
# spawn_activity_flush), so activity within 3x that cadence proves the session
# alive without spending a per-session GET each sweep tick. The 3x ratio is
# compile-time asserted in router/src/tasks.rs (mirrored constant there).
ACTIVITY_FRESH_WINDOW_SEC = 30.0

# ponytail: fixed direct-to-Appium probe fan-out width per host; was the
# general.probe_concurrency_per_host knob (deleted with the dial-out stages).
_PROBE_CONCURRENCY_PER_HOST = 4

SESSION_SYNC_WAKE_SOURCE_TOTAL = Counter(
    "gridfleet_session_sync_wake_source",
    "Why session_sync_loop ran a cycle: doorbell (bus event) or tick (timeout).",
    labelnames=("source",),
)
# Pre-register both wake sources: an absent series on a dashboard is
# indistinguishable from broken doorbell wiring; an explicit 0 is not.
for _wake_source in ("doorbell", "tick"):
    SESSION_SYNC_WAKE_SOURCE_TOTAL.labels(source=_wake_source)

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


# Cap keys carrying the client's negotiated idle contract. The W3C request uses
# the vendor-prefixed key; some driver responses echo it un-prefixed, so both
# spellings are read (prefixed first).
_NEW_COMMAND_TIMEOUT_KEYS = ("appium:newCommandTimeout", "newCommandTimeout")


def _client_new_command_timeout_sec(session: Session) -> int | None:
    """The client's negotiated ``newCommandTimeout`` in seconds, or ``None``.

    Prefers ``actual_capabilities`` (what Appium accepted at create) over
    ``requested_capabilities``. Boolean, negative, and non-numeric values are
    ignored. ``0`` is returned as ``0`` (Appium semantics: never idle-kill).
    """
    for caps in (session.actual_capabilities, session.requested_capabilities):
        if not isinstance(caps, dict):
            continue
        for key in _NEW_COMMAND_TIMEOUT_KEYS:
            value = caps.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
                continue
            return int(value)
    return None


def _effective_idle_timeout_sec(session: Session, *, idle_timeout: int, ceiling: int) -> int:
    """Per-session idle budget (7a): the operator timeout, extendable by the
    client's ``newCommandTimeout`` up to *ceiling*.

    * No client value -> ``idle_timeout`` unchanged.
    * ``newCommandTimeout: 0`` ("never") -> the ceiling (N14 zombie guarantee).
    * Otherwise the client may EXTEND the window up to the ceiling, never
      shorten it: drivers enforce short values themselves (uia2's 60s default,
      proven live by S21), so the sweep stays a backstop, not the contract
      enforcer — and a ceiling misconfigured below ``idle_timeout`` can never
      undercut the operator's window.
    """
    nct = _client_new_command_timeout_sec(session)
    if nct is None:
        return idle_timeout
    extension = ceiling if nct == 0 else min(nct, ceiling)
    return max(idle_timeout, extension)


async def _terminate_for_close(
    session_id: str, target: str | None, *, reap_reason: str | None, force_close: bool = False
) -> _LivenessVerdict:
    """Terminate the Appium session for a reap/node-stop close, returning the verdict.

    Caller holds the per-host semaphore and has already decided this session should
    close (``reap_reason is not None or node_stopped``). A ``defer`` verdict means the
    DB row must stay open (no resolvable target, or an unconfirmed terminate).

    ``force_close`` (probe rows only): close the DB row even when the terminate is
    unconfirmed or no target resolves. A probe row carries no client session and emits
    no ``session.ended`` event, so a blind close cannot strand a live foreign session —
    the reason the default is a conservative ``defer`` for client rows. Once the probe
    row is closed its id leaves ``live_session_predicate``, so ``_kill_orphans`` on a
    running node terminates any residual Appium session on the next tick. Without this a
    probe whose Appium endpoint died (the node respawned on a new port) defers every
    tick forever and pins its device un-allocatable (WS-16.1 leak).
    """
    action: _LivenessAction = "close_reap" if reap_reason is not None else "close_node_stopped"
    if target is None:
        # No resolvable Appium target at all: closing the DB row would
        # orphan a possibly-still-live Appium session (and _kill_orphans
        # also skips a None-target device), re-allocating the device while
        # the session keeps holding it. Defer to a later tick when a target
        # resolves rather than close blind (C3) — unless this is a probe row.
        if force_close:
            return _LivenessVerdict(action=action, reap_reason=reap_reason)
        return _LivenessVerdict(action="defer", reap_reason=reap_reason, defer_detail="no resolvable Appium target")
    if not await appium_direct.terminate_session(target, session_id):
        # Terminate unconfirmed (5xx/timeout): the Appium session may
        # still be alive. Closing the row would free the device for
        # re-allocation under the live foreign session (wave-5 #3) —
        # keep the row and retry next tick, mirroring run-release.
        # An already-gone session converges: terminate_session maps
        # 404 to True, so the retry closes it. A probe row force-closes
        # here instead (no client session to strand).
        if force_close:
            return _LivenessVerdict(action=action, reap_reason=reap_reason)
        return _LivenessVerdict(action="defer", reap_reason=reap_reason, defer_detail="Appium terminate failed")
    return _LivenessVerdict(action=action, reap_reason=reap_reason)


@dataclass(frozen=True, slots=True)
class _LivenessTarget:
    """Immutable per-session inputs for the liveness probe + finalize phases.

    Computed once, while the read session is still open, from eager-loaded ORM
    attributes only (no extra query). No ORM ``Session``/``Device`` survives past
    the read phase — only these scalars cross into the no-transaction Appium
    liveness effect and the later fresh finalize transaction (Task 7).
    """

    session_pk: uuid.UUID
    session_id: str
    device_id: uuid.UUID
    host_id: uuid.UUID
    target: str | None
    reap_reason: str | None
    node_stopped: bool
    force_close: bool
    skip_probe: bool
    started_at: datetime
    last_activity_at: datetime | None
    effective_idle_timeout_sec: int | None


@dataclass(frozen=True, slots=True)
class _LivenessLoad:
    targets: list[_LivenessTarget]
    grace: int
    idle_timeout: int


@dataclass(frozen=True, slots=True)
class _OrphanCandidate:
    device_id: uuid.UUID
    host_id: uuid.UUID
    target: str


@dataclass(frozen=True, slots=True)
class _OrphanTargets:
    candidates: list[_OrphanCandidate]
    known_ids_by_device: dict[uuid.UUID, set[str]]
    devices_with_pending: set[uuid.UUID]


def _session_factory_from_db(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Derive a fresh-session factory bound to the same engine as *db*.

    ``sync``'s injected ``db`` is used only for this: every read and write the
    sweep performs runs through its own short-lived session/transaction opened
    from the returned factory, so no transaction is ever held open across an
    Appium liveness/list/delete call (Task 7).
    """
    if db.bind is None:
        raise RuntimeError("Session sync session is not bound")
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


# Module-level wake hook (P2). The session_sync loop registers its running service's
# ``wake`` here on startup; other leader-owned loops (e.g. the allocation reaper, which
# runs in the same process as the leader's session_sync loop) call
# ``request_session_sync_wake`` to ring the doorbell after they free a device, so the
# sweep runs immediately instead of waiting up to one poll interval. In-process only:
# the reaper and the leader session_sync loop are co-located on the leader, so a direct
# in-memory hook is sufficient and avoids a bus round trip. The hook lives on a
# never-rebound holder so the setter mutates an attribute rather than a module global.
class _WakeDoorbell:
    fn: Callable[[], None] | None = None


_wake_doorbell = _WakeDoorbell()


def register_session_sync_wake_hook(hook: Callable[[], None]) -> None:
    _wake_doorbell.fn = hook


def request_session_sync_wake() -> None:
    """Ring the session_sync doorbell if a loop is registered; no-op otherwise."""
    if _wake_doorbell.fn is not None:
        _wake_doorbell.fn()


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

        This loop polls each device's Appium server directly
        (``app.grid.appium_direct``):

        1. Liveness — every running DB session is probed; a definitively dead
           one is closed through the same ended path the allocator uses. An
           indeterminate (network) verdict is left untouched: we never kill on
           uncertainty.
        2. Orphan kill — each running node is enumerated; any Appium session
           with no matching DB row (probe rows included) is terminated so a
           leaked session cannot pin a device busy forever.

        The loop never inserts or hydrates Session rows: row creation is owned
        by the allocation and probe paths.

        ``db`` is used only to derive a fresh session factory bound to the same
        engine (Task 7): the immutable sweep targets are loaded in one short
        read session that is closed before any Appium call, the liveness/list
        Appium effects then run with no DB session open at all, and each
        changed Session is finalized in its own fresh transaction. No
        transaction is ever held open across a remote call.
        """
        session_factory = _session_factory_from_db(db)
        async with session_factory() as read_db:
            liveness_load = await self._load_liveness_targets(read_db)
            orphan_targets = await self._load_orphan_targets(read_db)

        verdicts = await self._probe_liveness_targets(liveness_load.targets)
        await self._finalize_liveness(
            session_factory,
            liveness_load.targets,
            verdicts,
            grace=liveness_load.grace,
            idle_timeout=liveness_load.idle_timeout,
        )

        live_id_lists = await self._enumerate_orphan_targets(orphan_targets)
        await self._terminate_orphans(orphan_targets, live_id_lists)

    async def _load_liveness_targets(self, db: AsyncSession) -> _LivenessLoad:
        """Read every running DB-truth session and compute its immutable
        probe/finalize inputs, deciding the same three things the original inline
        sweep did:

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

        No further query is issued once this returns — the caller closes this
        read session before running the batched Appium liveness effects.
        """
        idle_timeout = self._settings.get_int("grid.session_idle_timeout_sec")
        idle_ceiling = self._settings.get_int("grid.session_idle_timeout_ceiling_sec")
        grace = self._settings.get_int("grid.session_first_command_grace_sec")
        now = now_utc()
        grace_cutoff = now - timedelta(seconds=grace)
        fresh_cutoff = now - timedelta(seconds=ACTIVITY_FRESH_WINDOW_SEC)

        running_stmt = (
            select(Session)
            .options(
                selectinload(Session.device).selectinload(Device.appium_node),
                selectinload(Session.device).selectinload(Device.host),
            )
            .where(
                Session.status == SessionStatus.running,
                Session.ended_at.is_(None),
            )
            .order_by(Session.id)
        )
        running_sessions = (await db.execute(running_stmt)).scalars().all()

        def _reap_reason(session: Session) -> tuple[str | None, int | None]:
            """Return why a session should be reaped (idle / never-commanded), else
            None, plus the effective idle timeout (for logging) when computed."""
            if session.last_activity_at is None:
                return ("never_commanded" if session.started_at < grace_cutoff else None), None
            effective = _effective_idle_timeout_sec(session, idle_timeout=idle_timeout, ceiling=idle_ceiling)
            return ("idle" if session.last_activity_at < now - timedelta(seconds=effective) else None), effective

        targets: list[_LivenessTarget] = []
        for session in running_sessions:
            device = session.device
            if device is None:
                continue
            node = device.appium_node
            node_stopped = node is not None and node.desired_state is not AppiumDesiredState.running
            reap_reason, effective_idle = _reap_reason(session)
            skip_probe = (
                reap_reason is None
                and not node_stopped
                and session.last_activity_at is not None
                and session.last_activity_at >= fresh_cutoff
            )
            targets.append(
                _LivenessTarget(
                    session_pk=session.id,
                    session_id=session.session_id,
                    device_id=device.id,
                    host_id=device.host_id,
                    target=resolve_router_target(session),
                    reap_reason=reap_reason,
                    node_stopped=node_stopped,
                    force_close=session.test_name == PROBE_TEST_NAME,
                    skip_probe=skip_probe,
                    started_at=session.started_at,
                    last_activity_at=session.last_activity_at,
                    effective_idle_timeout_sec=effective_idle,
                )
            )
        return _LivenessLoad(targets=targets, grace=grace, idle_timeout=idle_timeout)

    async def _probe_liveness_targets(self, targets: list[_LivenessTarget]) -> list[_LivenessVerdict]:
        """Gather one verdict per session concurrently, bounded by a per-host
        semaphore, so a single hung node cannot stall the sweep wall time (#10).
        No DB access happens here — only the Appium liveness/terminate calls."""
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = per_key_semaphores(_PROBE_CONCURRENCY_PER_HOST)

        async def _probe(t: _LivenessTarget) -> _LivenessVerdict:
            if t.skip_probe:
                # Router-flushed activity inside the freshness window: the session
                # was provably alive moments ago — skip the probe entirely (#19).
                return _LivenessVerdict(action="leave", reap_reason=None)
            async with host_semaphores[t.host_id]:
                if t.reap_reason is not None or t.node_stopped:
                    # A probe row (never-commanded past grace, or on a stopped node) whose
                    # Appium endpoint is unreachable would otherwise defer forever — the
                    # bounded probe create timeout means a still-running probe row this old
                    # has no live owner, so force the close (WS-16.1 leak).
                    return await _terminate_for_close(
                        t.session_id, t.target, reap_reason=t.reap_reason, force_close=t.force_close
                    )
                if t.target is None:
                    return _LivenessVerdict(action="leave", reap_reason=None)  # nothing to probe
                alive = await appium_direct.session_alive(t.target, t.session_id)
                if alive is None:
                    return _LivenessVerdict(action="leave_indeterminate", reap_reason=None)
                return _LivenessVerdict(action="leave" if alive else "close_dead", reap_reason=None)

        return list(await asyncio.gather(*[_probe(t) for t in targets]))

    async def _finalize_liveness(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        targets: list[_LivenessTarget],
        verdicts: list[_LivenessVerdict],
        *,
        grace: int,
        idle_timeout: int,
    ) -> None:
        device_ids_to_restore: set[uuid.UUID] = set()
        for target, verdict in zip(targets, verdicts, strict=True):
            if verdict.action == "defer":
                logger.warning(
                    "grid_session_reap_deferred session=%s device=%s reason=%s (%s)",
                    target.session_id,
                    target.device_id,
                    verdict.reap_reason or "node_stopped",
                    verdict.defer_detail,
                )
                continue
            if verdict.action == "leave":
                continue
            if verdict.action == "leave_indeterminate":
                logger.debug("session_liveness_indeterminate session=%s device=%s", target.session_id, target.device_id)
                continue
            # Every remaining action closes the DB row the same way a vanished session is
            # closed, so the session stops pinning the device busy.
            self._log_session_close(target, verdict, grace=grace, idle_timeout=idle_timeout)
            await self._close_session_locked(session_factory, target)
            device_ids_to_restore.add(target.device_id)

        for device_id in sorted(device_ids_to_restore):
            await self._restore_device_after_session_end(session_factory, device_id)

    def _log_session_close(
        self,
        target: _LivenessTarget,
        verdict: _LivenessVerdict,
        *,
        grace: int,
        idle_timeout: int,
    ) -> None:
        """Emit the close metric + warning for a reap / node-stopped verdict."""
        if verdict.action == "close_reap":
            if verdict.reap_reason == "never_commanded":
                GRID_NEVER_COMMANDED_SESSIONS_REAPED_TOTAL.inc()
                logger.warning(
                    "grid_never_commanded_session_reaped session=%s device=%s started_at=%s grace_sec=%s",
                    target.session_id,
                    target.device_id,
                    target.started_at.isoformat(),
                    grace,
                )
            else:
                GRID_IDLE_SESSIONS_REAPED_TOTAL.inc()
                logger.warning(
                    "grid_idle_session_reaped session=%s device=%s last_activity=%s idle_timeout_sec=%s "
                    "effective_idle_timeout_sec=%s",
                    target.session_id,
                    target.device_id,
                    target.last_activity_at.isoformat() if target.last_activity_at else None,
                    idle_timeout,
                    target.effective_idle_timeout_sec,
                )
        elif verdict.action == "close_node_stopped":
            GRID_NODE_STOPPED_SESSIONS_CLOSED_TOTAL.inc()
            logger.warning(
                "grid_node_stopped_session_closed session=%s device=%s",
                target.session_id,
                target.device_id,
            )

    async def _close_session_locked(
        self, session_factory: async_sessionmaker[AsyncSession], target: _LivenessTarget
    ) -> None:
        """Close one session in its own fresh transaction, acquiring the device lock
        the shared close helper requires (Device -> Session lock order)."""
        async with session_factory.begin() as db:
            locked = await device_locking.lock_device_handle(db, target.device_id)
            await session_service.close_running_session_locked(
                db, locked, session_pk=target.session_pk, publisher=self._publisher
            )
        logger.info("Session %s ended", target.session_id)

    async def _restore_device_after_session_end(
        self, session_factory: async_sessionmaker[AsyncSession], device_id: uuid.UUID
    ) -> None:
        """Per-device still-running check + lifecycle handler + restore.

        Runs across two fresh sessions. ``handle_session_finished`` is a sanctioned
        commit boundary (Phase 3: it commits internally), so the first session is
        deliberately plain (not ``begin()``) to avoid a double commit with its own
        boundary; the locked recheck + intent reconcile that may follow then run in
        their own fresh ``begin()`` transaction.
        """
        async with session_factory() as db:
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
        # NO_PENDING_OR_RECOVERED: restore availability via the normal session-end path.
        # Authoritative recheck under the row lock. ``handle_session_finished``
        # may have already derived the correct state; a fresh session inserted
        # between it and this lock must override that derivation. Always recheck.
        async with session_factory.begin() as db:
            locked_device = await device_locking.lock_device(db, device_id)
            # Mark dirty either way: the reconciler derives available/offline from
            # durable facts when no session remains, or restores busy when one does.
            await intent_service.IntentService(db).reconcile_now(locked_device.id, publisher=self._publisher)

    async def _load_orphan_targets(self, db: AsyncSession) -> _OrphanTargets:
        """Read the running-node candidates, their pending-row shield, and the known
        (running/pending) session ids per candidate device — everything the batched
        ``list_sessions``/``terminate_session`` effect phase needs. No further query
        is issued once this returns."""
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

        # A pending row marks an in-progress backend-owned create. Its Appium id is
        # not known until promotion, so unknown ids on that device are spared until
        # the reaper fails a crash-orphaned pending row; the next sweep then kills it.
        candidates: list[_OrphanCandidate] = []
        for device in devices:
            target = node_target(device)
            if target is None:
                continue
            candidates.append(_OrphanCandidate(device_id=device.id, host_id=device.host_id, target=target))

        candidate_ids = [candidate.device_id for candidate in candidates]
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

        # Resolve the known (running/pending) session ids for every candidate device in
        # one IN-query, grouped by device (#12), before the batched enumerate/terminate
        # effect phase runs — this used to run interleaved with list_sessions/terminate_session
        # (Task 7 closes that gap so no DB access happens once the effect phase starts).
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

        return _OrphanTargets(
            candidates=candidates,
            known_ids_by_device=dict(known_ids_by_device),
            devices_with_pending=devices_with_pending,
        )

    async def _enumerate_orphan_targets(self, targets: _OrphanTargets) -> list[list[str] | None]:
        """Enumerate every candidate node's live sessions concurrently, bounded per
        host, so a hung node cannot stall the sweep wall time (#10). No DB access
        happens here — only the ``list_sessions`` Appium call."""
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = per_key_semaphores(_PROBE_CONCURRENCY_PER_HOST)

        async def _enumerate(candidate: _OrphanCandidate) -> list[str] | None:
            async with host_semaphores[candidate.host_id]:
                return await appium_direct.list_sessions(candidate.target)

        return list(await asyncio.gather(*[_enumerate(c) for c in targets.candidates]))

    async def _terminate_orphans(self, targets: _OrphanTargets, live_id_lists: list[list[str] | None]) -> None:
        """Terminate Appium sessions with no tracking DB row, serially on the session."""
        for candidate, live_ids in zip(targets.candidates, live_id_lists, strict=True):
            if live_ids is None:
                GRID_ORPHAN_ENUM_UNAVAILABLE_TOTAL.inc()
                logger.warning(
                    "grid_orphan_enum_unavailable device=%s target=%s", candidate.device_id, candidate.target
                )
                continue
            if not live_ids:
                continue
            known_ids = targets.known_ids_by_device.get(candidate.device_id, set())
            for live_id in live_ids:
                if live_id in known_ids:
                    continue
                if candidate.device_id in targets.devices_with_pending:
                    # Unknown id on a pending device may be the in-progress create.
                    continue
                terminated = await appium_direct.terminate_session(candidate.target, live_id)
                if terminated:
                    GRID_ORPHAN_SESSIONS_KILLED_TOTAL.inc()
                logger.warning(
                    "grid_orphan_session_killed session=%s device=%s target=%s terminated=%s",
                    live_id,
                    candidate.device_id,
                    candidate.target,
                    terminated,
                )
