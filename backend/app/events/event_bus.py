from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, bindparam, func, select
from sqlalchemy import event as sa_event
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import register_gauge_refresher
from app.core.metrics_recorders import ACTIVE_SSE_CONNECTIONS, record_event_published, record_outbox_gap_retired
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.events.catalog import (
    PUBLIC_EVENT_NAME_SET,
    EventSeverity,
    allowed_severities_for,
    default_severity_for,
)
from app.events.models import SystemEvent
from app.events.outbox_schema import NOTIFY_CHANNEL

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
    from sqlalchemy.orm import Session

logger = get_logger(__name__)

LISTENER_POLL_INTERVAL_SEC = 5
LISTENER_RECONNECT_DELAY_SEC = 1.0
LISTENER_READY_TIMEOUT_SEC = 5.0
HANDLER_DRAIN_TIMEOUT_SEC = 5.0
POLL_SCAN_CHUNK_SIZE = 500

# The ``idle_in_transaction_session_timeout`` both compose files set on the
# postgres service. Mirrored here, not read from the database, so the derivation
# below is visible at the constant;
# backend/tests/contracts/test_compose_config.py pins the two together.
IDLE_IN_TRANSACTION_BOUND_SEC = 60.0
# A gap id is retired unresolved at this age. The bound that matters is how long
# a transaction can hold a ``system_events`` sequence value without committing,
# which is what the Postgres setting above caps; doubling it is headroom for a
# transaction that is slow rather than idle (nothing caps that -- there is
# deliberately no ``statement_timeout``). Retiring early strands a row; retiring
# late costs one dict entry, so the multiple biases high. Retiring on a
# transaction-id horizon instead would reproduce the hole this design closes:
# the horizon can settle while the row is still uncommitted.
GAP_RETIREMENT_SAFETY_MULTIPLE = 2.0
GAP_RETIREMENT_SEC = IDLE_IN_TRANSACTION_BOUND_SEC * GAP_RETIREMENT_SAFETY_MULTIPLE
# How long a dispatched row id stays in the dedupe map after the frontier passes
# it. Covers a ``NOTIFY`` that reaches the listener well after the poller
# already delivered the row -- the listener reloads by id and has no frontier to
# check, so the map is the only thing that can suppress the second dispatch. The
# map is bounded by event rate over this window (ints in a dict), not by
# promotion lag.
DEDUPE_GRACE_SEC = 300.0

# One statement, one plan, regardless of how many gaps are outstanding: an
# expanding ``IN`` recompiles per arity, ``= ANY(:gap_ids)`` does not.
_GAP_LOOKUP_SQL = (
    select(SystemEvent)
    .where(SystemEvent.id == func.any(bindparam("gap_ids", type_=ARRAY(BigInteger))))
    .order_by(SystemEvent.id.asc())
)


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    timestamp: str = field(default_factory=lambda: now_utc().isoformat())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    severity: EventSeverity = "neutral"

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "data": self.data,
        }

    @classmethod
    def from_system_event(cls, row: SystemEvent) -> Event:
        if row.severity is not None:
            severity: EventSeverity = row.severity  # type: ignore[assignment]
        elif row.type in PUBLIC_EVENT_NAME_SET:
            severity = default_severity_for(row.type)
        else:
            severity = "neutral"
        return cls(
            type=row.type,
            data=row.data,
            timestamp=row.created_at.isoformat(),
            id=row.event_id,
            severity=severity,
        )


def build_event(
    event_type: str,
    data: dict[str, Any],
    *,
    severity: EventSeverity | None = None,
) -> Event:
    """Validate and construct the single Event envelope for one emission.

    This is the one place an event is *emitted*, so it is also the one place
    the published counter is incremented. Loaders rebuild envelopes through
    ``Event.from_system_event`` and must not count again: with N workers each
    reloading every committed row, counting at dispatch would multiply
    ``gridfleet_events_published_total`` by the worker count. The tradeoff is
    that an emission whose transaction later rolls back is still counted.
    """
    if event_type in PUBLIC_EVENT_NAME_SET:
        resolved: EventSeverity = severity if severity is not None else default_severity_for(event_type)
        allowed = allowed_severities_for(event_type)
        if resolved not in allowed:
            raise ValueError(f"severity {resolved!r} not allowed for {event_type!r}; allowed: {sorted(allowed)!r}")
    else:
        resolved = severity if severity is not None else "neutral"
    record_event_published(event_type)
    return Event(type=event_type, data=data, severity=resolved)


def stage_system_event(db: AsyncSession | Session, event: Event) -> SystemEvent:
    """Add the outbox row to the caller's transaction; never flush or commit here.

    A transaction takes its ``system_events`` sequence value at flush time but
    is assigned a transaction id only by its first *write*, so a row can hold id
    ``j`` while its transaction has no xid at all. Nothing here has to care: the
    poller records any id its forward scan passed over as a gap and resolves it
    by direct lookup, so visibility is the only thing delivery consults. This
    used to depend on the device row-lock contract stamping ``xmax`` before
    anything was staged; it no longer does (see ``app.devices.locking``).
    """
    row = SystemEvent(event_id=event.id, type=event.type, data=event.data, severity=event.severity)
    db.add(row)
    return row


EventHandler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self, max_queue_size: int = 256, log_buffer_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._log: deque[Event] = deque(maxlen=log_buffer_size)
        self._max_queue_size = max_queue_size
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._engine: AsyncEngine | None = None
        self._handlers: list[EventHandler] = []
        self._listener_task: asyncio.Task[None] | None = None
        self._poller_task: asyncio.Task[None] | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._listener_ready = asyncio.Event()
        self._last_seen_system_event_id = 0
        # Gap row id -> monotonic time first observed. These are the ids a
        # forward scan passed over: their transactions had taken a sequence
        # value but had not committed, including one whose first write is the
        # outbox INSERT and therefore holds no transaction id at all. Recording
        # them is what makes unconditional promotion safe.
        self._pending_gaps: dict[int, float] = {}
        # Dispatched row id -> monotonic time dispatched. This is the delivery
        # dedupe window, deliberately separate from ``_log``: ``_log`` is a
        # fixed-size display buffer for ``snapshot()`` and the in-memory
        # fallback, so a burst larger than it (an offline cascade, say) would
        # silently lose dedupe. Bounded by ``DEDUPE_GRACE_SEC`` instead.
        self._dispatched_row_ids: dict[int, float] = {}
        self._dispatch_lock = asyncio.Lock()
        self._started = False

    def configure(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._engine = engine

    async def start(self) -> None:
        """Register LISTEN before seeding the watermark, then start the poller.

        The order closes the boot gap without extra bookkeeping: a row visible
        at seed time is below the seed, and a row still in flight at seed time
        commits after the listener registered, so its notification is caught. A
        listener that cannot register within ``LISTENER_READY_TIMEOUT_SEC``
        degrades to poller-only rather than blocking application startup — the
        reconnect loop keeps retrying behind it.
        """
        if self._started or self._session_factory is None or self._engine is None:
            return
        self._listener_ready = asyncio.Event()
        listener_task = asyncio.create_task(self._listen_for_notifications())
        self._listener_task = listener_task
        try:
            try:
                await asyncio.wait_for(self._listener_ready.wait(), timeout=LISTENER_READY_TIMEOUT_SEC)
            except TimeoutError:
                logger.warning(
                    "System event listener did not register within %ss; seeding the watermark without it. "
                    "Rows staged but uncommitted right now land below the seed with no listener to catch "
                    "their NOTIFY, so they can only arrive if the listener reconnects before they commit.",
                    LISTENER_READY_TIMEOUT_SEC,
                )
            self._last_seen_system_event_id = await self._read_latest_row_id()
            self._prune_dispatched_row_ids()
        except BaseException:
            # Never orphan the listener: ``_started`` stays False on this path,
            # so ``shutdown`` would have nothing to cancel it with.
            listener_task.cancel()
            await asyncio.gather(listener_task, return_exceptions=True)
            self._listener_task = None
            raise
        self._poller_task = asyncio.create_task(self._poll_for_missed_events())
        self._started = True

    async def shutdown(self) -> None:
        cancellable_tasks = [task for task in (self._listener_task, self._poller_task) if task is not None]
        for task in cancellable_tasks:
            task.cancel()
        if cancellable_tasks:
            cancelled_results = await asyncio.gather(*cancellable_tasks, return_exceptions=True)
            for task, result in zip(cancellable_tasks, cancelled_results, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    continue
                if isinstance(result, BaseException):
                    logger.error(
                        "Event bus task %s failed during shutdown",
                        task.get_name(),
                        exc_info=(type(result), result, result.__traceback__),
                    )
        await self._shutdown_handler_tasks()
        self._listener_task = None
        self._poller_task = None
        self._started = False

    def register_handler(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._max_queue_size)
        self._subscribers.add(q)
        logger.info("SSE client subscribed (total: %d)", len(self._subscribers))
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)
        logger.info("SSE client unsubscribed (total: %d)", len(self._subscribers))

    def track_task(self, task: asyncio.Task[None]) -> None:
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    def queue_for_session(
        self,
        db: AsyncSession | Session,
        event_type: str,
        data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> SystemEvent | None:
        """Stage an event row in the caller's open source transaction.

        Accepts either an ``AsyncSession`` or the underlying sync ``Session`` —
        callers that pull the session out of ``inspect(obj).session`` get the
        sync object directly and can pass it without reconstructing the
        ``AsyncSession``.

        The row is added but never flushed, committed, or dispatched here: the
        caller's commit makes it durable and the database trigger notifies
        listeners, so a rollback (or savepoint rollback) drops the event with
        the change that caused it. An unconfigured bus has no outbox and falls
        back to the in-memory after-commit queue, returning ``None`` instead of
        the staged row. ``data`` is captured by reference — do not mutate it
        after queuing.
        """
        event = build_event(event_type, data, severity=severity)
        if self._session_factory is None:
            self._queue_fallback_event(db, event)
            return None
        return stage_system_event(db, event)

    def _queue_fallback_event(self, db: AsyncSession | Session, event: Event) -> None:
        """In-memory after-commit queue for an unconfigured (non-persistent) bus.

        The running loop is captured at registration time (when this method is
        called from inside an awaited coroutine). This is strictly safer than
        resolving the loop inside the after_commit hook itself, which can fire
        from non-greenlet contexts (sync fixture teardown) where
        ``asyncio.get_running_loop()`` would raise ``RuntimeError``.
        """
        sync_session = db.sync_session if isinstance(db, AsyncSession) else db
        if sync_session.get_transaction() is None:
            # Ride a real transaction the way persistent staging does, where
            # ``db.add`` autobegins one. Without it ``Session.rollback()`` has
            # nothing to roll back and fires no drop hook, so the queue would
            # survive to be dispatched by the next unrelated commit.
            sync_session.begin()
        loop = asyncio.get_running_loop()

        pending: list[Event] = sync_session.info.setdefault(_PENDING_EVENTS_KEY, [])
        pending.append(event)

        if sync_session.info.get(_PENDING_EVENTS_LISTENER_KEY):
            return
        sync_session.info[_PENDING_EVENTS_LISTENER_KEY] = True

        def _flush_on_commit(_session: object) -> None:
            events: list[Event] = sync_session.info.pop(_PENDING_EVENTS_KEY, [])
            sync_session.info.pop(_PENDING_EVENTS_LISTENER_KEY, None)
            if not events:
                return
            # ``loop.create_task`` (not ``asyncio.create_task``): after_commit is a sync
            # SQLAlchemy callback that can fire with no running loop.
            task = loop.create_task(_dispatch_pending_fallback(events, self))
            self.track_task(task)

        def _drop_on_rollback(_session: object) -> None:
            sync_session.info.pop(_PENDING_EVENTS_KEY, None)
            sync_session.info.pop(_PENDING_EVENTS_LISTENER_KEY, None)

        # ``once=True`` makes SQLAlchemy auto-remove the listener after firing —
        # avoids deque-mutation hazards if anything tried ``sa_event.remove`` from
        # inside the callback.
        sa_event.listen(sync_session, "after_commit", _flush_on_commit, once=True)
        sa_event.listen(sync_session, "after_rollback", _drop_on_rollback, once=True)

    async def publish(self, event_type: str, data: dict[str, Any], severity: EventSeverity | None = None) -> None:
        """Publish a standalone event that cannot join a source transaction.

        Persistent mode owns one short outbox transaction and then waits for the
        listener or poller to deliver the committed row — it never dispatches
        locally, so every worker sees the event exactly the same way.
        """
        event = build_event(event_type, data, severity=severity)
        if self._session_factory is None:
            self._remember_and_dispatch(event)
            return
        async with self._session_factory.begin() as db:
            stage_system_event(db, event)

    async def get_recent_events_persisted(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if self._session_factory is None:
            events = list(self._log)
            if event_types:
                events = [event for event in events if event.type in event_types]
            if severities:
                events = [event for event in events if event.severity in severities]
            events.reverse()
            total = len(events)
            items = events[offset : offset + limit]
            return [event.to_dict() for event in items], total
        async with self._session_factory() as db:
            stmt = select(SystemEvent)
            if event_types:
                stmt = stmt.where(SystemEvent.type.in_(event_types))
            if severities:
                stmt = stmt.where(SystemEvent.severity.in_(severities))
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = int((await db.execute(count_stmt)).scalar_one())
            stmt = stmt.order_by(SystemEvent.id.desc()).offset(offset).limit(limit)
            result = await db.execute(stmt)
            events = [Event.from_system_event(row) for row in result.scalars().all()]
        return [event.to_dict() for event in events], total

    def snapshot(self) -> dict[str, Any]:
        return {
            "subscriber_count": len(self._subscribers),
            "recent_events": [event.to_dict() for event in self._log],
            "persistent_mode": self._session_factory is not None,
            "started": self._started,
        }

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def _read_latest_row_id(self) -> int:
        if self._session_factory is None:
            return 0
        async with self._session_factory() as db:
            result = await db.execute(select(func.max(SystemEvent.id)))
            return int(result.scalar() or 0)

    async def _dispatch_handlers(self, event: Event) -> None:
        for handler in self._handlers:
            try:
                result = handler(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception("System event handler failed for %s", event.type)

    def _remember_and_dispatch(self, event: Event) -> None:
        self._log.append(event)
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Dropping event for slow SSE client")
        task = asyncio.create_task(self._dispatch_handlers(event))
        self.track_task(task)

    async def _shutdown_handler_tasks(self, timeout: float = HANDLER_DRAIN_TIMEOUT_SEC) -> None:
        # Handler tasks can spawn additional handler tasks (e.g. an
        # ``after_commit`` callback awaits ``publisher.publish`` which schedules
        # ``_dispatch_handlers`` as a new tracked task). A one-shot
        # ``asyncio.wait`` snapshot would let those children outlive shutdown
        # and contend with ``DROP SCHEMA CASCADE`` in tests. Drain in a loop
        # until the set quiesces or the deadline expires.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            tasks = {task for task in self._handler_tasks if not task.done()}
            if not tasks:
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                logger.warning("Cancelling %d system event handler task(s) during shutdown", len(tasks))
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                break
            await asyncio.wait(tasks, timeout=remaining)
        self._handler_tasks.clear()

    def _prune_dispatched_row_ids(self) -> None:
        """Forget dedupe entries older than the grace period.

        Dropping an entry the moment the frontier passed it is what let a late
        ``NOTIFY`` re-dispatch an already-delivered row.
        """
        cutoff = time.monotonic() - DEDUPE_GRACE_SEC
        self._dispatched_row_ids = {
            row_id: dispatched_at
            for row_id, dispatched_at in self._dispatched_row_ids.items()
            if dispatched_at > cutoff
        }

    def _dispatch_new_rows(self, rows: list[SystemEvent]) -> None:
        """Dispatch every row this process has not delivered yet.

        Membership is checked, recorded, and acted on with no ``await`` in
        between, so a listener delivery that landed during the scan is visible
        here and cannot produce a second dispatch.
        """
        now = time.monotonic()
        for row in rows:
            row_id = int(row.id)
            if row_id in self._dispatched_row_ids:
                continue
            self._dispatched_row_ids[row_id] = now
            self._pending_gaps.pop(row_id, None)
            self._remember_and_dispatch(Event.from_system_event(row))

    def _record_new_gaps(self, observed: set[int], frontier: int) -> None:
        """Remember every id in the scanned interval the scan did not return.

        Those are exactly the stranding candidates: a transaction had taken the
        sequence value and had not committed when the scan ran. Visibility is the
        only thing consulted -- no transaction id is involved, so the case that
        defeats every xid horizon (a transaction whose first write is this
        INSERT, which holds a sequence value before it holds an xid) is covered
        by construction.

        A rolled-back transaction's value is recorded too and never resolves;
        ``_resolve_pending_gaps`` retires it on the time bound. Rows the
        data-cleanup job deletes are far below the frontier -- retention is days
        and the poller is seconds behind -- so cleanup does not manufacture gaps.
        """
        now = time.monotonic()
        for row_id in range(self._last_seen_system_event_id + 1, frontier + 1):
            if row_id in observed or row_id in self._dispatched_row_ids or row_id in self._pending_gaps:
                continue
            self._pending_gaps[row_id] = now

    async def _resolve_pending_gaps(self, db: AsyncSession) -> list[SystemEvent]:
        """Look up every remembered gap in one statement; retire the aged-out ones.

        Runs before the forward scan so a large backlog of new rows cannot starve
        it. The dict is mutated only after the query returns, so a failed query
        cannot drop an entry.
        """
        if not self._pending_gaps:
            return []
        gap_ids = sorted(self._pending_gaps)
        rows = list((await db.execute(_GAP_LOOKUP_SQL, {"gap_ids": gap_ids})).scalars().all())
        resolved = {int(row.id) for row in rows}
        now = time.monotonic()
        for gap_id in gap_ids:
            if gap_id in resolved:
                continue
            first_seen = self._pending_gaps.get(gap_id)
            if first_seen is None or now - first_seen < GAP_RETIREMENT_SEC:
                continue
            self._pending_gaps.pop(gap_id, None)
            record_outbox_gap_retired()
            logger.warning(
                "Retiring unresolved system_events gap id %s after %.0fs; its transaction rolled back, "
                "or it outlived the idle-in-transaction bound GAP_RETIREMENT_SEC is derived from. "
                "If this metric fires in normal operation, the bound or the derivation is wrong.",
                gap_id,
                GAP_RETIREMENT_SEC,
            )
        return rows

    async def _load_and_dispatch_system_event(self, row_id: int) -> None:
        """Dispatch one notified row without touching the frontier.

        Ids are handed out by the sequence at flush time but become visible at
        commit time, so a notification for a higher id says nothing about lower
        ids still in flight. ``_dispatch_and_promote`` is the only writer of the
        frontier.

        This path deliberately does not take ``_dispatch_lock``: a poll holds it
        across DB round trips, and a poll can itself reach this method (a
        notification delivered mid-scan). The two mutations below are single-key
        ``dict`` operations with no ``await`` between the membership check and
        the write, which is what makes them safe on one event loop.
        """
        if self._session_factory is None:
            return
        async with self._session_factory() as db:
            row = await db.get(SystemEvent, row_id)
            if row is None:
                return
            event = Event.from_system_event(row)
        if row_id in self._dispatched_row_ids:
            return
        self._dispatched_row_ids[row_id] = time.monotonic()
        # A gap the listener resolves needs no second lookup.
        self._pending_gaps.pop(row_id, None)
        self._remember_and_dispatch(event)

    async def _scan_window(self, db: AsyncSession) -> tuple[list[SystemEvent], set[int], int]:
        """Page forward from the frontier; return undispatched rows, every id seen, and the top id.

        Returning the observed ids is what lets the caller compute the gaps: an
        id in the scanned interval that the scan did not return is a candidate.

        The window is walked in ``POLL_SCAN_CHUNK_SIZE`` keyset pages so no
        single statement materialises an unbounded result set. Chunking bounds
        the statement, not the window: a plain ``LIMIT`` on the frontier
        predicate would now be safe -- unconditional promotion means the next
        poll continues past the page -- but it would drain a backlog one page per
        poll instead of one poll, so the paging stays.

        The dedupe read here only avoids hydrating rows this process already
        delivered; it is not the delivery guard. This method awaits, so the
        authoritative check is in ``_dispatch_new_rows``.
        """
        cursor = self._last_seen_system_event_id
        pending: list[SystemEvent] = []
        observed: set[int] = set()
        while True:
            page = (
                await db.execute(
                    select(SystemEvent.id)
                    .where(SystemEvent.id > cursor)
                    .order_by(SystemEvent.id.asc())
                    .limit(POLL_SCAN_CHUNK_SIZE)
                )
            ).all()
            if not page:
                return pending, observed, cursor
            page_ids = [int(row.id) for row in page]
            observed.update(page_ids)
            undispatched = [row_id for row_id in page_ids if row_id not in self._dispatched_row_ids]
            if undispatched:
                hydrated = (
                    await db.execute(
                        select(SystemEvent).where(SystemEvent.id.in_(undispatched)).order_by(SystemEvent.id.asc())
                    )
                ).scalars()
                pending.extend(hydrated.all())
            cursor = page_ids[-1]
            if len(page) < POLL_SCAN_CHUNK_SIZE:
                return pending, observed, cursor

    async def _dispatch_missed_events(self) -> None:
        """Resolve gaps, scan forward, record new gaps, promote the frontier.

        Promotion is unconditional. Advancing past an uncommitted id is safe
        because the same poll recorded it as a gap, and gap resolution consults
        visibility only -- nothing in this path reads a transaction id. Two
        designs that did are gone: one gated on a snapshot's upper bound, which
        is ``latestCompletedXid + 1`` and so can sit at or below a still-running
        transaction; the other gated on the id assigned to the poll's own
        transaction, which cannot cover a row whose transaction took the
        sequence value before ``heap_insert`` gave it an xid at all.
        ``tests/contracts/test_no_transaction_id_reasoning.py`` names both
        withdrawn gates and is what keeps them from coming back -- including
        back into this docstring, which is why they are not named here.

        The body is serialised, but not for delivery safety. The tail that
        dispatches, records gaps, writes the frontier and prunes contains no
        ``await``, so no concurrent body can interleave with it, and
        ``_record_new_gaps`` reads the frontier live rather than from a pre-scan
        snapshot -- which together make stranding unreachable with or without the
        lock. What the lock buys is that the unconditional frontier write cannot
        *regress*: a body that scanned before a peer promoted would otherwise
        drag the frontier backwards, costing a re-scan of rows that are all
        already in the dedupe map. That is work amplification, not data loss, so
        this is serialisation for monotonicity and for holding a doorbell wake to
        one scan per tick rather than one per caller. Pinned by
        ``tests/events/test_event_bus_gaps.py::test_the_lock_keeps_the_frontier_monotonic``.
        """
        if self._session_factory is None:
            return
        async with self._dispatch_lock:
            await self._dispatch_and_promote()

    async def _dispatch_and_promote(self) -> None:
        """The serialised body of ``_dispatch_missed_events``; never call it directly."""
        if self._session_factory is None:
            return
        async with self._session_factory() as db:
            try:
                gap_rows = await self._resolve_pending_gaps(db)
            except SQLAlchemyError as exc:
                # Give up the whole poll rather than scan forward on a database
                # that just failed a read. The gap set is untouched, so the next
                # poll retries exactly the same ids.
                logger.warning("Could not resolve outbox gaps (%s); retrying on the next poll", exc)
                return
            # Dispatched before the scan: if the scan then fails, these rows are
            # already in the dedupe map and the retry will not duplicate them.
            self._dispatch_new_rows(gap_rows)
            rows, observed, frontier = await self._scan_window(db)
        self._dispatch_new_rows(rows)
        self._record_new_gaps(observed, frontier)
        self._last_seen_system_event_id = frontier
        self._prune_dispatched_row_ids()

    async def _listen_for_notifications(self) -> None:
        # The unconfigured-bus return stays outside the loop: inside it, a bus
        # with no engine would spin through the reconnect sleep forever.
        if self._engine is None:
            return
        while True:
            try:
                await self._listen_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("System event listener connection failed; reconnecting")
            await asyncio.sleep(LISTENER_RECONNECT_DELAY_SEC)

    async def _listen_once(self) -> None:
        if self._engine is None:
            return
        queue: asyncio.Queue[int] = asyncio.Queue()

        async with self._engine.connect() as conn:
            raw_conn = await conn.get_raw_connection()
            driver_conn = raw_conn.driver_connection
            if driver_conn is None:
                return

            def callback(
                _driver: object,
                _pid: int,
                _channel: str,
                payload: str,
            ) -> None:
                try:
                    row_id = int(payload)
                except ValueError:
                    return
                queue.put_nowait(row_id)

            await driver_conn.add_listener(NOTIFY_CHANNEL, callback)
            self._listener_ready.set()
            try:
                while True:
                    row_id = await queue.get()
                    await self._load_and_dispatch_system_event(row_id)
            finally:
                with contextlib.suppress(Exception):
                    await driver_conn.remove_listener(NOTIFY_CHANNEL, callback)

    async def _poll_for_missed_events(self) -> None:
        while True:
            try:
                await self._dispatch_missed_events()
            except Exception:
                logger.exception("System event poller failed")
            await asyncio.sleep(LISTENER_POLL_INTERVAL_SEC)


def register_events_gauge_refresher(bus: EventBus) -> None:
    """Register a gauge refresher that reads subscriber_count from the given bus.

    Called once at startup — the closure captures the bus instance, avoiding
    module-level mutable state.
    """

    async def _refresh(db: object) -> None:
        del db
        ACTIVE_SSE_CONNECTIONS.set(bus.subscriber_count)

    register_gauge_refresher(_refresh)


_PENDING_EVENTS_KEY = "_pending_event_bus_events"
_PENDING_EVENTS_LISTENER_KEY = "_pending_event_bus_events_listener"


async def _dispatch_pending_fallback(events: list[Event], bus: EventBus) -> None:
    """Dispatch fallback-mode events that survived to commit.

    Dispatches directly rather than re-entering ``publish`` so a session reused
    after a rollback can never republish a discarded event.
    """
    for event in events:
        try:
            bus._remember_and_dispatch(event)
        except Exception:
            logger.exception("Failed to dispatch deferred event %s", event.type)
