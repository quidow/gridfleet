from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.events import Event, EventBus
from app.events.models import SystemEvent
from tests.helpers import drain_handlers, recent_events


def _session_factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    assert db_session.bind is not None
    return async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)


async def test_start_and_shutdown_manage_listener_tasks(db_session: AsyncSession) -> None:
    bus = EventBus()
    session_factory = _session_factory(db_session)
    engine = cast("object", db_session.bind)
    assert engine is not None
    bus.configure(session_factory=session_factory, engine=cast("object", engine))

    async def wait_forever() -> None:
        await asyncio.Event().wait()

    async def ready_then_wait_forever() -> None:
        # ``start`` waits for the listener to register before seeding the
        # watermark; a stand-in that never signals would stall it.
        bus._listener_ready.set()
        await wait_forever()

    with (
        patch.object(bus, "_read_latest_row_id", new=AsyncMock(return_value=7)),
        patch.object(bus, "_listen_for_notifications", new=ready_then_wait_forever),
        patch.object(bus, "_poll_for_missed_events", new=wait_forever),
    ):
        await bus.start()
        assert bus.snapshot()["started"] is True
        assert bus._last_seen_system_event_id == 7
        await bus.shutdown()

    assert bus.snapshot()["started"] is False


async def test_read_latest_row_id_and_noop_paths(db_session: AsyncSession) -> None:
    bus = EventBus()
    await bus.start()
    assert await bus._read_latest_row_id() == 0
    await bus._load_and_dispatch_system_event(1)
    await bus._dispatch_missed_events()
    await bus._listen_for_notifications()

    session_factory = _session_factory(db_session)
    bus.configure(session_factory=session_factory, engine=db_session.bind)
    assert await bus._read_latest_row_id() == 0
    await bus._load_and_dispatch_system_event(999999)


async def test_load_system_event_dispatches_new_event(db_session: AsyncSession) -> None:
    bus = EventBus()
    session_factory = _session_factory(db_session)
    bus.configure(session_factory=session_factory, engine=db_session.bind)
    row = SystemEvent(event_id="evt-new", type="demo", data={"n": 1})
    db_session.add(row)
    await db_session.commit()

    await bus._load_and_dispatch_system_event(int(row.id))

    assert recent_events(bus)[-1]["id"] == "evt-new"


async def test_publish_persists_and_reads_recent_events(db_session: AsyncSession) -> None:
    bus = EventBus()
    session_factory = _session_factory(db_session)
    engine = db_session.bind
    assert engine is not None
    bus.configure(session_factory=session_factory, engine=engine)

    await bus.publish("device.created", {"device_id": "1"})
    await bus.publish("device.updated", {"device_id": "1"})

    persisted, total = await bus.get_recent_events_persisted(limit=1, event_types=["device.updated"])

    assert total == 1
    assert persisted[0]["type"] == "device.updated"
    # Persistent publish only stages; the in-memory log fills when the poller reloads.
    assert recent_events(bus) == []
    await bus._dispatch_missed_events()
    assert recent_events(bus, limit=2)[-1]["type"] == "device.updated"


async def test_get_recent_events_persisted_falls_back_to_in_memory_log() -> None:
    bus = EventBus()
    await bus.publish("device.created", {"id": "1"})
    await bus.publish("session.started", {"id": "2"})

    events, total = await bus.get_recent_events_persisted(limit=1, offset=0, event_types=["session.started"])

    assert total == 1
    assert events == [recent_events(bus, event_types=["session.started"])[0]]


async def test_load_system_event_skips_duplicate_entries(db_session: AsyncSession) -> None:
    bus = EventBus()
    session_factory = _session_factory(db_session)
    engine = db_session.bind
    assert engine is not None
    bus.configure(session_factory=session_factory, engine=engine)

    await bus.publish("device.created", {"device_id": "1"})
    row_id = await db_session.scalar(select(SystemEvent.id))
    assert row_id is not None
    await bus._load_and_dispatch_system_event(int(row_id))

    original = recent_events(bus)
    assert len(original) == 1
    await bus._load_and_dispatch_system_event(int(row_id))

    assert recent_events(bus) == original


async def test_dispatch_missed_events_loads_new_rows_and_skips_duplicates(db_session: AsyncSession) -> None:
    bus = EventBus()
    session_factory = _session_factory(db_session)
    engine = db_session.bind
    assert engine is not None
    bus.configure(session_factory=session_factory, engine=engine)

    row_a = SystemEvent(event_id="evt-a", type="a", data={"n": 1})
    row_b = SystemEvent(event_id="evt-b", type="b", data={"n": 2})
    db_session.add_all([row_a, row_b])
    await db_session.commit()

    # Deliver row A the way a notification would, so the poll's dedupe has a
    # dispatched row id to skip.
    await bus._load_and_dispatch_system_event(int(row_a.id))
    await bus._dispatch_missed_events()

    assert [event["id"] for event in recent_events(bus, limit=10)] == ["evt-a", "evt-b"]
    # Promotion is unconditional now, so the frontier itself is deterministic:
    # nothing another session holds open can hold it back.
    assert bus._last_seen_system_event_id >= int(row_b.id)


async def test_shutdown_handler_tasks_cancels_pending_tasks() -> None:
    bus = EventBus()

    async def never_finishes() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(never_finishes())
    bus._handler_tasks.add(task)

    await bus._shutdown_handler_tasks(timeout=0)

    assert task.cancelled() or task.done()
    assert bus._handler_tasks == set()


async def test_shutdown_handler_tasks_drains_tasks_spawned_during_shutdown() -> None:
    # ``after_commit`` hooks (queue_for_session) spawn a tracked task
    # whose body awaits ``event_bus.publish``, which in turn schedules another
    # tracked task via ``_remember_and_dispatch``. Shutdown must await the
    # chain — otherwise the child task survives and may run queries against a
    # schema the caller is about to drop.
    bus = EventBus()
    child_done = asyncio.Event()

    async def child() -> None:
        await asyncio.sleep(0.05)
        child_done.set()

    async def outer(_: Event) -> None:
        task = asyncio.create_task(child())
        bus._handler_tasks.add(task)
        task.add_done_callback(bus._handler_tasks.discard)

    bus.register_handler(outer)
    bus._remember_and_dispatch(Event(type="demo", data={}))

    await bus._shutdown_handler_tasks()

    assert child_done.is_set()
    assert bus._handler_tasks == set()


async def test_dispatch_handlers_logs_and_continues_on_handler_error() -> None:
    bus = EventBus()
    received: list[str] = []

    def bad_handler(_: Event) -> None:
        raise RuntimeError("boom")

    async def good_handler(event: Event) -> None:
        received.append(event.type)

    bus.register_handler(bad_handler)
    bus.register_handler(good_handler)

    await bus._dispatch_handlers(Event(type="demo", data={}))

    assert received == ["demo"]


async def test_remember_and_dispatch_drops_event_for_full_subscriber_queue() -> None:
    bus = EventBus(max_queue_size=1)
    subscriber = bus.subscribe()
    await subscriber.put(Event(type="existing", data={}))

    bus._remember_and_dispatch(Event(type="demo", data={}))
    await bus._shutdown_handler_tasks(timeout=1)

    assert subscriber.qsize() == 1


async def test_listen_for_notifications_dispatches_valid_payload_and_removes_listener() -> None:
    bus = EventBus()
    driver_connection = SimpleNamespace()
    callbacks: dict[str, object] = {}

    async def add_listener(channel: str, callback: object) -> None:
        callbacks["channel"] = channel
        callbacks["callback"] = callback
        callback(None, 0, channel, "bad")
        callback(None, 0, channel, "7")
        callback(None, 0, channel, "8")

    async def remove_listener(channel: str, callback: object) -> None:
        callbacks["removed"] = (channel, callback)

    driver_connection.add_listener = add_listener
    driver_connection.remove_listener = remove_listener

    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
            return False

        async def get_raw_connection(self) -> SimpleNamespace:
            return SimpleNamespace(driver_connection=driver_connection)

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

    bus._engine = cast("object", FakeEngine())

    with (
        patch.object(
            bus,
            "_load_and_dispatch_system_event",
            new=AsyncMock(side_effect=[None, asyncio.CancelledError()]),
        ) as loader,
        pytest.raises(asyncio.CancelledError),
    ):
        await bus._listen_for_notifications()

    assert callbacks["channel"] == "system_events"
    assert loader.await_args_list[0].args == (7,)
    assert callbacks["removed"][0] == "system_events"


async def test_listen_once_returns_when_driver_connection_missing() -> None:
    bus = EventBus()

    class FakeConnection:
        async def __aenter__(self) -> FakeConnection:
            return self

        async def __aexit__(self, _exc_type: object, _exc: object, _tb: object) -> bool:
            return False

        async def get_raw_connection(self) -> SimpleNamespace:
            return SimpleNamespace(driver_connection=None)

    class FakeEngine:
        def connect(self) -> FakeConnection:
            return FakeConnection()

    bus._engine = cast("object", FakeEngine())
    # ``_listen_once`` directly: the reconnect wrapper would retry this forever.
    await bus._listen_once()


async def test_listen_for_notifications_returns_when_engine_missing() -> None:
    bus = EventBus()
    await bus._listen_for_notifications()


async def test_poll_for_missed_events_logs_exceptions_and_sleeps() -> None:
    bus = EventBus()

    with (
        patch.object(
            bus,
            "_dispatch_missed_events",
            new=AsyncMock(side_effect=[RuntimeError("boom"), asyncio.CancelledError()]),
        ),
        patch("app.events.event_bus.asyncio.sleep", new=AsyncMock()) as sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await bus._poll_for_missed_events()

    sleep.assert_awaited()


@pytest.mark.db
async def test_listener_loads_committed_triggered_row_before_dispatch(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)

    def mine() -> list[Event]:
        return [event for event in received if event.data.get("device_id") == "listener"]

    await bus.start()
    try:
        bus.queue_for_session(db_session, "device.updated", {"device_id": "listener"})
        await asyncio.sleep(0)
        assert mine() == [], "notification escaped before commit"
        await db_session.commit()
        for _ in range(500):
            if mine():
                break
            await asyncio.sleep(0.01)
        assert [event.data for event in mine()] == [{"device_id": "listener"}]
    finally:
        await bus.shutdown()


@pytest.mark.db
async def test_poller_recovers_committed_row_when_notify_wake_up_is_lost(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)
    await bus.start()
    try:
        assert bus._listener_task is not None
        bus._listener_task.cancel()
        await asyncio.gather(bus._listener_task, return_exceptions=True)
        bus._listener_task = None
        bus.queue_for_session(db_session, "device.updated", {"device_id": "poller"})
        await db_session.commit()
        await bus._dispatch_missed_events()
        await drain_handlers(bus)
        await bus._dispatch_missed_events()
        await drain_handlers(bus)
        assert [event.data["device_id"] for event in received] == ["poller"]
    finally:
        await bus.shutdown()


@pytest.mark.db
async def test_poller_delivers_row_committed_after_a_higher_id(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """A long transaction's lower id must survive a shorter transaction committing first."""
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)

    async with db_session_maker() as slow, db_session_maker() as fast:
        bus.queue_for_session(slow, "device.updated", {"device_id": "slow"})
        await slow.flush()  # id assigned by the sequence, still invisible
        bus.queue_for_session(fast, "device.updated", {"device_id": "fast"})
        await fast.commit()  # higher id, commits first

        await bus._dispatch_missed_events()
        await drain_handlers(bus)
        assert [event.data["device_id"] for event in received] == ["fast"]

        await slow.commit()
        await bus._dispatch_missed_events()
        await drain_handlers(bus)

    assert sorted(event.data["device_id"] for event in received) == ["fast", "slow"]


@pytest.mark.db
async def test_poller_pages_the_whole_window_in_one_poll(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Chunking bounds the statement, not the window.

    Every committed row above the frontier is delivered in a single poll, in
    ``POLL_SCAN_CHUNK_SIZE`` keyset pages. A plain ``LIMIT`` on the frontier
    predicate would deliver one page per poll instead -- correct, since
    promotion is unconditional, but slower to drain a backlog.
    """
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)

    rows = [bus.queue_for_session(db_session, "device.updated", {"device_id": f"d{index}"}) for index in range(5)]
    await db_session.flush()
    assert all(row is not None for row in rows)
    top_id = max(int(row.id) for row in rows if row is not None)
    await db_session.commit()

    with patch("app.events.event_bus.POLL_SCAN_CHUNK_SIZE", 2):
        await bus._dispatch_missed_events()
        await drain_handlers(bus)

    assert sorted(event.data["device_id"] for event in received) == ["d0", "d1", "d2", "d3", "d4"]
    assert bus._last_seen_system_event_id == top_id
    assert bus._pending_gaps == {}


@pytest.mark.db
async def test_poll_does_not_redispatch_a_row_the_listener_delivered_mid_scan(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Delivery dedupe must be read at dispatch time, not snapshotted before the scan.

    ``_scan_window`` awaits at least twice before ``_dispatch_missed_events``
    reaches its dispatch loop, and the frontier is written only after the
    dispatch loop, so a row the listener delivers between the scan and the
    dispatch must still be dispatched exactly once.
    """
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    received: list[Event] = []
    bus.register_handler(received.append)

    row = bus.queue_for_session(db_session, "device.updated", {"device_id": "mid-scan"})
    assert row is not None
    await db_session.flush()
    row_id = int(row.id)
    await db_session.commit()

    scan_window = bus._scan_window

    async def scan_then_deliver(db: AsyncSession) -> Any:  # noqa: ANN401
        result = await scan_window(db)
        # The NOTIFY lands while the poller sits between its scan and its
        # dispatch loop — the exact window a pre-scan snapshot would miss.
        await bus._load_and_dispatch_system_event(row_id)
        return result

    with patch.object(bus, "_scan_window", new=scan_then_deliver):
        await bus._dispatch_missed_events()
    await drain_handlers(bus)

    assert [event.data["device_id"] for event in received] == ["mid-scan"]


@pytest.mark.db
async def test_listener_reconnects_after_connection_loss(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=cast("AsyncEngine", db_session.bind))
    attempts = 0
    original = bus._listen_once

    async def flaky() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            bus._listener_ready.set()
            raise ConnectionResetError("simulated server restart")
        await original()

    with (
        patch.object(bus, "_listen_once", new=flaky),
        patch("app.events.event_bus.LISTENER_RECONNECT_DELAY_SEC", 0.01),
    ):
        await bus.start()
        try:
            for _ in range(200):
                if attempts >= 2:
                    break
                await asyncio.sleep(0.01)
            assert attempts >= 2, "listener did not reconnect after a dropped connection"
        finally:
            await bus.shutdown()


async def test_listener_reconnect_logging_backs_off_during_an_outage(caplog: pytest.LogCaptureFixture) -> None:
    """A database outage must not produce one traceback per second per worker.

    The reconnect delay stays at 1s -- the listener should come back promptly
    and the 5s poller is only a backstop -- so the burst is absorbed by the
    logging, which reports how many failures it suppressed.
    """
    bus = EventBus()
    bus.configure(session_factory=None, engine=cast("AsyncEngine", object()))
    attempts = 0

    async def always_failing() -> None:
        nonlocal attempts
        attempts += 1
        if attempts > 5:
            raise asyncio.CancelledError
        raise ConnectionResetError("database is down")

    with (
        patch.object(bus, "_listen_once", new=always_failing),
        patch("app.events.event_bus.asyncio.sleep", new=AsyncMock()),
        caplog.at_level(logging.ERROR),
        pytest.raises(asyncio.CancelledError),
    ):
        await bus._listen_for_notifications()

    reports = [record for record in caplog.records if "listener connection failed" in record.getMessage()]
    assert len(reports) == 1, f"expected one report for a burst of 5 failures, got {len(reports)}"
    assert "suppressed" in reports[0].getMessage()
