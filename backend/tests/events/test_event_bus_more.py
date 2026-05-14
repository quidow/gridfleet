from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.events import Event, EventBus
from app.events.models import SystemEvent


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

    with (
        patch.object(bus, "_read_latest_row_id", new=AsyncMock(return_value=7)),
        patch.object(bus, "_listen_for_notifications", new=wait_forever),
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

    assert bus.get_recent_events()[-1]["id"] == "evt-new"


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
    assert bus.get_recent_events(limit=2)[-1]["type"] == "device.updated"


async def test_get_recent_events_persisted_falls_back_to_in_memory_log() -> None:
    bus = EventBus()
    await bus.publish("device.created", {"id": "1"})
    await bus.publish("session.started", {"id": "2"})

    events, total = await bus.get_recent_events_persisted(limit=1, offset=0, event_types=["session.started"])

    assert total == 1
    assert events == [bus.get_recent_events(event_types=["session.started"])[0]]


async def test_load_system_event_skips_duplicate_entries(db_session: AsyncSession) -> None:
    bus = EventBus()
    session_factory = _session_factory(db_session)
    engine = db_session.bind
    assert engine is not None
    bus.configure(session_factory=session_factory, engine=engine)

    await bus.publish("device.created", {"device_id": "1"})
    row_id = await db_session.scalar(select(SystemEvent.id))
    assert row_id is not None

    original = bus.get_recent_events()
    await bus._load_and_dispatch_system_event(int(row_id))

    assert bus.get_recent_events() == original


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

    bus._remember_and_dispatch(Event(type="a", data={"n": 1}, id="evt-a"))
    await bus._dispatch_missed_events()

    assert [event["id"] for event in bus.get_recent_events(limit=10)] == ["evt-a", "evt-b"]
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


async def test_remember_and_dispatch_handles_full_subscriber_and_webhook_queues() -> None:
    bus = EventBus(max_queue_size=1)
    subscriber = bus.subscribe()
    await subscriber.put(Event(type="existing", data={}))
    webhook_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=1)
    await webhook_queue.put(Event(type="existing", data={}))
    bus.set_webhook_queue(webhook_queue)

    bus._remember_and_dispatch(Event(type="demo", data={}))
    await bus._shutdown_handler_tasks(timeout=1)

    assert subscriber.qsize() == 1
    assert webhook_queue.qsize() == 1


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


async def test_listen_for_notifications_returns_when_driver_connection_missing() -> None:
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
    await bus._listen_for_notifications()


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
