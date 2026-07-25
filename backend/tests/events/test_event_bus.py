import asyncio
from typing import cast

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.events import Event, EventBus
from app.events.models import SystemEvent
from tests.helpers import drain_handlers, recent_events, reset_event_bus
from tests.helpers import test_event_bus as event_bus


def _session_bind_engine(session: AsyncSession) -> AsyncEngine:
    assert session.bind is not None
    return cast("AsyncEngine", session.bind)


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

    events = recent_events(bus, limit=2)
    assert len(events) == 2
    assert events[0]["data"]["n"] == 2
    assert events[1]["data"]["n"] == 3


async def test_get_recent_events_filter_types() -> None:
    bus = EventBus()
    await bus.publish("device.created", {"n": 1})
    await bus.publish("session.started", {"n": 2})
    await bus.publish("device.updated", {"n": 3})

    events = recent_events(bus, event_types=["device.created", "device.updated"])
    assert len(events) == 2
    assert events[0]["type"] == "device.created"
    assert events[1]["type"] == "device.updated"


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
    await bus.publish("device.updated", {"device_id": "123"})

    snapshot = bus.snapshot()
    assert snapshot["subscriber_count"] == 1
    assert snapshot["recent_events"][0]["type"] == "device.updated"

    reset_event_bus(bus)
    assert bus.subscriber_count == 0
    assert recent_events(bus) == []
    assert queue.qsize() == 1


@pytest.mark.db
async def test_publish_without_severity_stages_catalog_default(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Publishing a catalog event without severity= should stage the catalog default."""
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=_session_bind_engine(db_session))

    # host.registered has default_severity="success"
    await bus.publish("host.registered", {"host_id": "h1", "hostname": "h1.local", "status": "online"})

    row = (await db_session.execute(select(SystemEvent))).scalar_one()
    assert row.severity == "success"
    assert Event.from_system_event(row).to_dict()["severity"] == "success"


async def test_publish_override_must_be_allowed() -> None:
    """Publishing with a severity not in allowed_severities should raise ValueError."""
    # host.registered allows only {"success", "info"}; "critical" is not allowed
    with pytest.raises(ValueError, match=r"not allowed for 'host\.registered'"):
        await EventBus().publish("host.registered", {"host_id": "h1"}, severity="critical")


@pytest.mark.db
async def test_publish_allowed_override_stages_that_severity(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    """Publishing with a valid override severity should stage that severity."""
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=_session_bind_engine(db_session))

    # device.operational_state_changed allows ALL_SEVERITIES including "success"
    await bus.publish(
        "device.operational_state_changed",
        {"device_id": "d1", "device_name": "D1", "old_operational_state": "online", "new_operational_state": "offline"},
        severity="success",
    )

    row = (await db_session.execute(select(SystemEvent))).scalar_one()
    assert row.severity == "success"
    d = Event.from_system_event(row).to_dict()
    assert d["severity"] == "success"
    # Verify it appears at top level (not nested inside data)
    assert "severity" not in d["data"]


async def test_from_system_event_falls_back_to_default() -> None:
    """SystemEvent row with severity=None should use catalog default_severity_for the type."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.type = "device.operational_state_changed"
    row.data = {"device_id": "d1", "device_name": "D1"}
    row.event_id = "evt-001"
    row.severity = None
    row.created_at.isoformat.return_value = "2024-01-01T00:00:00+00:00"

    event = Event.from_system_event(row)

    # device.operational_state_changed has default_severity="info"
    assert event.severity == "info"


@pytest.mark.db
async def test_queue_for_session_carries_severity(db_session: AsyncSession) -> None:
    """queue_for_session stages the severity kwarg onto the outbox row."""
    event_bus.queue_for_session(
        db_session,
        "host.status_changed",
        {"host_id": "h1", "old_status": "online", "new_status": "offline"},
        severity="warning",
    )
    await db_session.commit()

    row = (await db_session.execute(select(SystemEvent))).scalar_one()
    assert row.type == "host.status_changed"
    assert row.data == {"host_id": "h1", "old_status": "online", "new_status": "offline"}
    assert row.severity == "warning"


@pytest.mark.db
async def test_queue_for_session_dispatches_only_once_reloaded(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=_session_bind_engine(db_session))
    received: list[Event] = []
    bus.register_handler(received.append)

    row = bus.queue_for_session(db_session, "device.updated", {"id": "x"})
    assert row is not None
    await db_session.commit()
    await drain_handlers(bus)
    assert received == []  # committing does not dispatch locally

    await bus._load_and_dispatch_system_event(int(row.id))
    await drain_handlers(bus)
    assert [(event.type, event.data) for event in received] == [("device.updated", {"id": "x"})]


async def test_queue_for_session_fallback_dispatches_after_commit(db_session: AsyncSession) -> None:
    bus = EventBus()
    received: list[Event] = []
    bus.register_handler(received.append)

    assert bus.queue_for_session(db_session, "device.updated", {"id": "x"}) is None
    assert received == []  # nothing before commit
    await db_session.commit()
    await drain_handlers(bus)  # let the after-commit task run
    assert [(event.type, event.data) for event in received] == [("device.updated", {"id": "x"})]


async def test_event_bus_shutdown_waits_for_inflight_handlers(db_session: AsyncSession) -> None:
    engine = _session_bind_engine(db_session)
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
    await event_bus._dispatch_missed_events()  # persistent publish only stages; the poller dispatches
    await asyncio.wait_for(started.wait(), 1)

    shutdown_task = asyncio.create_task(event_bus.shutdown())
    await asyncio.sleep(0)
    assert not shutdown_task.done()

    release.set()
    await asyncio.wait_for(shutdown_task, 1)

    assert completed.is_set()


@pytest.mark.db
async def test_queue_rollback_leaves_no_outbox_row_or_dispatch(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=_session_bind_engine(db_session))
    received: list[Event] = []
    bus.register_handler(received.append)

    bus.queue_for_session(db_session, "device.updated", {"device_id": "rolled-back"})
    await db_session.rollback()
    await bus._dispatch_missed_events()

    assert await db_session.scalar(select(func.count()).select_from(SystemEvent)) == 0
    assert received == []


@pytest.mark.db
async def test_queue_savepoint_rollback_keeps_only_surviving_outbox_row(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=_session_bind_engine(db_session))

    bus.queue_for_session(db_session, "device.updated", {"device_id": "survivor"})
    nested = await db_session.begin_nested()
    bus.queue_for_session(db_session, "device.updated", {"device_id": "discarded"})
    await nested.rollback()
    await db_session.commit()

    rows = (await db_session.execute(select(SystemEvent).order_by(SystemEvent.id))).scalars().all()
    assert [row.data["device_id"] for row in rows] == ["survivor"]


@pytest.mark.db
async def test_queue_validates_before_staging(db_session: AsyncSession) -> None:
    with pytest.raises(ValueError, match=r"not allowed for 'host\.registered'"):
        EventBus().queue_for_session(db_session, "host.registered", {"host_id": "h"}, severity="critical")
    assert await db_session.scalar(select(func.count()).select_from(SystemEvent)) == 0


async def test_fallback_rollback_does_not_dispatch_on_later_session_reuse(db_session: AsyncSession) -> None:
    bus = EventBus()
    received: list[Event] = []
    bus.register_handler(received.append)

    bus.queue_for_session(db_session, "device.updated", {"device_id": "rolled-back"})
    await db_session.rollback()
    async with db_session.begin():
        await db_session.execute(select(1))
    await drain_handlers(bus)

    assert received == []


@pytest.mark.db
async def test_persistent_publish_stages_without_local_dispatch(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    bus = EventBus()
    bus.configure(session_factory=db_session_maker, engine=_session_bind_engine(db_session))
    received: list[Event] = []
    bus.register_handler(received.append)

    await bus.publish("device.created", {"device_id": "standalone"})
    await drain_handlers(bus)
    assert received == []
    assert await db_session.scalar(select(func.count()).select_from(SystemEvent)) == 1

    await bus._dispatch_missed_events()
    await drain_handlers(bus)
    assert [event.data["device_id"] for event in received] == ["standalone"]
