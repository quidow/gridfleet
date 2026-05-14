from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import event as sa_event
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session  # noqa: TC002

from app.core.metrics import register_gauge_refresher
from app.core.metrics_recorders import ACTIVE_SSE_CONNECTIONS, record_event_published
from app.core.observability import get_logger
from app.events.models import SystemEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

logger = get_logger(__name__)

NOTIFY_CHANNEL = "system_events"
LISTENER_POLL_INTERVAL_SEC = 5
HANDLER_DRAIN_TIMEOUT_SEC = 5.0


@dataclass
class Event:
    type: str
    data: dict[str, Any]
    timestamp: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "id": self.id,
            "timestamp": self.timestamp,
            "data": self.data,
        }

    @classmethod
    def from_system_event(cls, row: SystemEvent) -> Event:
        return cls(type=row.type, data=row.data, timestamp=row.created_at.isoformat(), id=row.event_id)


EventHandler = Callable[[Event], Awaitable[None] | None]


class EventBus:
    def __init__(self, max_queue_size: int = 256, log_buffer_size: int = 1000) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._log: deque[Event] = deque(maxlen=log_buffer_size)
        self._max_queue_size = max_queue_size
        self._webhook_queue: asyncio.Queue[Event] | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._engine: AsyncEngine | None = None
        self._handlers: list[EventHandler] = []
        self._listener_task: asyncio.Task[None] | None = None
        self._poller_task: asyncio.Task[None] | None = None
        self._handler_tasks: set[asyncio.Task[None]] = set()
        self._last_seen_system_event_id = 0
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
        if self._started or self._session_factory is None or self._engine is None:
            return
        self._last_seen_system_event_id = await self._read_latest_row_id()
        self._listener_task = asyncio.create_task(self._listen_for_notifications())
        self._poller_task = asyncio.create_task(self._poll_for_missed_events())
        self._started = True

    async def drain_handlers(self) -> None:
        tasks = {task for task in self._handler_tasks if not task.done()}
        if tasks:
            await asyncio.gather(*tasks)

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

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        event = Event(type=event_type, data=data)
        record_event_published(event_type)
        if self._session_factory is not None:
            await self._persist_system_event(event)
        self._remember_and_dispatch(event)

    async def get_recent_events_persisted(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        event_types: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        if self._session_factory is None:
            events = list(self._log)
            if event_types:
                events = [event for event in events if event.type in event_types]
            events.reverse()
            total = len(events)
            items = events[offset : offset + limit]
            return [event.to_dict() for event in items], total
        async with self._session_factory() as db:
            stmt = select(SystemEvent)
            if event_types:
                stmt = stmt.where(SystemEvent.type.in_(event_types))
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = int((await db.execute(count_stmt)).scalar_one())
            stmt = stmt.order_by(SystemEvent.id.desc()).offset(offset).limit(limit)
            result = await db.execute(stmt)
            events = [Event.from_system_event(row) for row in result.scalars().all()]
        return [event.to_dict() for event in events], total

    def get_recent_events(self, limit: int = 100, event_types: list[str] | None = None) -> list[dict[str, Any]]:
        events = list(self._log)
        if event_types:
            events = [event for event in events if event.type in event_types]
        return [event.to_dict() for event in events[-limit:]]

    def set_webhook_queue(self, q: asyncio.Queue[Event]) -> None:
        self._webhook_queue = q

    def snapshot(self) -> dict[str, Any]:
        return {
            "subscriber_count": len(self._subscribers),
            "recent_events": [event.to_dict() for event in self._log],
            "webhook_queue_configured": self._webhook_queue is not None,
            "persistent_mode": self._session_factory is not None,
            "started": self._started,
        }

    def reset(self) -> None:
        self._subscribers.clear()
        self._log.clear()
        self._webhook_queue = None
        self._handlers.clear()
        for task in list(self._handler_tasks):
            task.cancel()
        self._handler_tasks.clear()
        self._session_factory = None
        self._engine = None
        self._last_seen_system_event_id = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def _read_latest_row_id(self) -> int:
        if self._session_factory is None:
            return 0
        async with self._session_factory() as db:
            result = await db.execute(select(func.max(SystemEvent.id)))
            return int(result.scalar() or 0)

    async def _persist_system_event(self, event: Event) -> None:
        assert self._session_factory is not None
        async with self._session_factory() as db:
            row = SystemEvent(event_id=event.id, type=event.type, data=event.data)
            db.add(row)
            await db.flush()
            self._last_seen_system_event_id = max(self._last_seen_system_event_id, int(row.id))
            await db.execute(
                text("SELECT pg_notify(:channel, :payload)"),
                {"channel": NOTIFY_CHANNEL, "payload": str(row.id)},
            )
            await db.commit()

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
        if self._webhook_queue is not None:
            try:
                self._webhook_queue.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("Webhook queue full, dropping event")
        task = asyncio.create_task(self._dispatch_handlers(event))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _shutdown_handler_tasks(self, timeout: float = HANDLER_DRAIN_TIMEOUT_SEC) -> None:
        tasks = {task for task in self._handler_tasks if not task.done()}
        if not tasks:
            self._handler_tasks.clear()
            return

        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("Cancelling %d system event handler task(s) during shutdown", len(pending))
            for task in pending:
                task.cancel()

        await asyncio.gather(*done, *pending, return_exceptions=True)
        self._handler_tasks.clear()

    async def _load_and_dispatch_system_event(self, row_id: int) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as db:
            row = await db.get(SystemEvent, row_id)
            if row is None:
                return
            event = Event.from_system_event(row)
        if any(existing.id == event.id for existing in self._log):
            self._last_seen_system_event_id = max(self._last_seen_system_event_id, row_id)
            return
        self._last_seen_system_event_id = max(self._last_seen_system_event_id, row_id)
        self._remember_and_dispatch(event)

    async def _dispatch_missed_events(self) -> None:
        if self._session_factory is None:
            return
        async with self._session_factory() as db:
            stmt = (
                select(SystemEvent)
                .where(SystemEvent.id > self._last_seen_system_event_id)
                .order_by(SystemEvent.id.asc())
            )
            result = await db.execute(stmt)
            rows = result.scalars().all()
        for row in rows:
            event = Event.from_system_event(row)
            if any(existing.id == event.id for existing in self._log):
                self._last_seen_system_event_id = max(self._last_seen_system_event_id, int(row.id))
                continue
            self._last_seen_system_event_id = max(self._last_seen_system_event_id, int(row.id))
            self._remember_and_dispatch(event)

    async def _listen_for_notifications(self) -> None:
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


event_bus = EventBus()


async def _refresh_events_gauges(db: object) -> None:
    del db
    ACTIVE_SSE_CONNECTIONS.set(event_bus.subscriber_count)


register_gauge_refresher(_refresh_events_gauges)

_PENDING_EVENTS_KEY = "_pending_event_bus_events"
_PENDING_EVENTS_LISTENER_KEY = "_pending_event_bus_events_listener"


async def _publish_pending_events(events: list[tuple[str, dict[str, Any]]]) -> None:
    for event_type, data in events:
        try:
            await event_bus.publish(event_type, data)
        except Exception:
            logger.exception("Failed to publish deferred event %s", event_type)


def queue_event_for_session(
    db: AsyncSession | Session,
    event_type: str,
    data: dict[str, Any],
) -> None:
    """Queue an event to dispatch after the outer transaction commits.

    Accepts either an ``AsyncSession`` or the underlying sync ``Session`` —
    callers that pull the session out of ``inspect(obj).session`` get the
    sync object directly and can pass it without reconstructing the
    ``AsyncSession``.

    On commit, ``loop.create_task(event_bus.publish(event_type, data))`` runs
    for each queued event. On rollback, the queue is dropped — webhook/SSE
    subscribers never see a transition that did not become durable. ``data``
    is captured by reference — do not mutate it after queuing.

    The running loop is captured at registration time (when this function is
    called from inside an awaited coroutine). This is strictly safer than
    resolving the loop inside the after_commit hook itself, which can fire
    from non-greenlet contexts (sync fixture teardown) where
    ``asyncio.get_running_loop()`` would raise ``RuntimeError``.
    """
    sync_session = db.sync_session if isinstance(db, AsyncSession) else db
    loop = asyncio.get_running_loop()

    pending: list[tuple[str, dict[str, Any]]] = sync_session.info.setdefault(_PENDING_EVENTS_KEY, [])
    pending.append((event_type, data))

    if sync_session.info.get(_PENDING_EVENTS_LISTENER_KEY):
        return
    sync_session.info[_PENDING_EVENTS_LISTENER_KEY] = True

    def _flush_on_commit(_session: object) -> None:
        events: list[tuple[str, dict[str, Any]]] = sync_session.info.pop(_PENDING_EVENTS_KEY, [])
        sync_session.info.pop(_PENDING_EVENTS_LISTENER_KEY, None)
        if not events:
            return
        task = loop.create_task(_publish_pending_events(events))
        event_bus._handler_tasks.add(task)
        task.add_done_callback(event_bus._handler_tasks.discard)

    def _drop_on_rollback(_session: object) -> None:
        sync_session.info.pop(_PENDING_EVENTS_KEY, None)
        sync_session.info.pop(_PENDING_EVENTS_LISTENER_KEY, None)

    # ``once=True`` makes SQLAlchemy auto-remove the listener after firing —
    # avoids deque-mutation hazards if anything tried ``sa_event.remove`` from
    # inside the callback.
    sa_event.listen(sync_session, "after_commit", _flush_on_commit, once=True)
    sa_event.listen(sync_session, "after_rollback", _drop_on_rollback, once=True)


def queue_device_crashed_event(
    db: AsyncSession | Session,
    *,
    device_id: str,
    device_name: str,
    source: str,
    reason: str,
    will_restart: bool,
    process: str | None = None,
) -> None:
    """Queue ``device.crashed`` to dispatch after the outer transaction commits."""
    queue_event_for_session(
        db,
        "device.crashed",
        {
            "device_id": device_id,
            "device_name": device_name,
            "source": source,
            "reason": reason,
            "will_restart": will_restart,
            "process": process,
        },
    )
