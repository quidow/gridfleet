import asyncio
from typing import TYPE_CHECKING, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.event_bus import Event, EventBus, event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine


def test_event_to_dict() -> None:
    event = Event(type="test.event", data={"key": "value"})
    d = event.to_dict()
    assert d["type"] == "test.event"
    assert d["data"] == {"key": "value"}
    assert "id" in d
    assert "timestamp" in d


async def test_publish_and_subscribe() -> None:
    bus = EventBus()
    queue = bus.subscribe()
    await bus.publish("device.created", {"device_id": "123"})

    event = queue.get_nowait()
    assert event.type == "device.created"
    assert event.data["device_id"] == "123"


async def test_unsubscribe() -> None:
    bus = EventBus()
    queue = bus.subscribe()
    assert bus.subscriber_count == 1
    bus.unsubscribe(queue)
    assert bus.subscriber_count == 0


async def test_full_queue_drops_event() -> None:
    bus = EventBus(max_queue_size=1)
    queue = bus.subscribe()
    await bus.publish("e1", {"n": 1})
    await bus.publish("e2", {"n": 2})  # should be dropped

    assert queue.qsize() == 1
    event = queue.get_nowait()
    assert event.data["n"] == 1


async def test_get_recent_events() -> None:
    bus = EventBus()
    await bus.publish("a", {"n": 1})
    await bus.publish("b", {"n": 2})
    await bus.publish("c", {"n": 3})

    events = bus.get_recent_events(limit=2)
    assert len(events) == 2
    assert events[0]["data"]["n"] == 2
    assert events[1]["data"]["n"] == 3


async def test_get_recent_events_filter_types() -> None:
    bus = EventBus()
    await bus.publish("device.created", {"n": 1})
    await bus.publish("session.started", {"n": 2})
    await bus.publish("device.updated", {"n": 3})

    events = bus.get_recent_events(event_types=["device.created", "device.updated"])
    assert len(events) == 2
    assert events[0]["type"] == "device.created"
    assert events[1]["type"] == "device.updated"


async def test_webhook_queue() -> None:
    bus = EventBus()
    wh_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=100)
    bus.set_webhook_queue(wh_queue)

    await bus.publish("test.event", {"key": "val"})

    event = wh_queue.get_nowait()
    assert event.type == "test.event"


async def test_subscriber_count() -> None:
    bus = EventBus()
    assert bus.subscriber_count == 0
    q1 = bus.subscribe()
    assert bus.subscriber_count == 1
    q2 = bus.subscribe()
    assert bus.subscriber_count == 2
    bus.unsubscribe(q1)
    bus.unsubscribe(q2)
    assert bus.subscriber_count == 0


async def test_snapshot_and_reset() -> None:
    bus = EventBus()
    queue = bus.subscribe()
    webhook_queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=10)
    bus.set_webhook_queue(webhook_queue)
    await bus.publish("device.updated", {"device_id": "123"})

    snapshot = bus.snapshot()
    assert snapshot["subscriber_count"] == 1
    assert snapshot["webhook_queue_configured"] is True
    assert snapshot["recent_events"][0]["type"] == "device.updated"

    bus.reset()
    assert bus.subscriber_count == 0
    assert bus.get_recent_events() == []
    assert bus.snapshot()["webhook_queue_configured"] is False
    assert queue.qsize() == 1


async def test_event_bus_shutdown_waits_for_inflight_handlers(db_session: AsyncSession) -> None:
    assert db_session.bind is not None
    engine = cast("AsyncEngine", db_session.bind)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    event_bus.configure(session_factory=session_factory, engine=engine)

    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def slow_handler(_: Event) -> None:
        started.set()
        await release.wait()
        completed.set()

    event_bus.register_handler(slow_handler)

    await event_bus.publish("test.event", {"value": "demo"})
    await asyncio.wait_for(started.wait(), 1)

    shutdown_task = asyncio.create_task(event_bus.shutdown())
    await asyncio.sleep(0)
    assert not shutdown_task.done()

    release.set()
    await asyncio.wait_for(shutdown_task, 1)

    assert completed.is_set()
