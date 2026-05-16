import asyncio
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.events import Event, EventBus, event_bus


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


async def test_publish_without_severity_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing a catalog event without severity= should resolve to the catalog default."""
    bus = EventBus()
    bus._session_factory = object()  # type: ignore[assignment]  # triggers persist branch

    captured: list[Event] = []

    async def fake_persist(event: Event) -> None:
        captured.append(event)

    monkeypatch.setattr(bus, "_persist_system_event", fake_persist)

    # host.registered has default_severity="success"
    await bus.publish("host.registered", {"host_id": "h1", "hostname": "h1.local", "status": "online"})

    assert len(captured) == 1
    assert captured[0].severity == "success"
    assert captured[0].to_dict()["severity"] == "success"


async def test_publish_override_must_be_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing with a severity not in allowed_severities should raise ValueError."""
    bus = EventBus()
    bus._session_factory = object()  # type: ignore[assignment]

    async def fake_persist(event: Event) -> None:
        pass

    monkeypatch.setattr(bus, "_persist_system_event", fake_persist)

    # host.registered allows only {"success", "info"}; "critical" is not allowed
    with pytest.raises(ValueError, match=r"not allowed for 'host\.registered'"):
        await bus.publish("host.registered", {"host_id": "h1"}, severity="critical")


async def test_publish_allowed_override_persists(monkeypatch: pytest.MonkeyPatch) -> None:
    """Publishing with a valid override severity should persist that severity."""
    bus = EventBus()
    bus._session_factory = object()  # type: ignore[assignment]

    captured: list[Event] = []

    async def fake_persist(event: Event) -> None:
        captured.append(event)

    monkeypatch.setattr(bus, "_persist_system_event", fake_persist)

    # device.operational_state_changed allows ALL_SEVERITIES including "success"
    await bus.publish(
        "device.operational_state_changed",
        {"device_id": "d1", "device_name": "D1", "old_operational_state": "online", "new_operational_state": "offline"},
        severity="success",
    )

    assert len(captured) == 1
    assert captured[0].severity == "success"
    d = captured[0].to_dict()
    assert d["severity"] == "success"
    # Verify it appears at top level (not nested inside data)
    assert "severity" in d
    assert "severity" not in d.get("data", {})


async def test_from_system_event_falls_back_to_default() -> None:
    """SystemEvent row with severity=None should use catalog default_severity_for the type."""
    from unittest.mock import MagicMock

    row = MagicMock()
    row.type = "device.hold_changed"
    row.data = {"device_id": "d1", "device_name": "D1"}
    row.event_id = "evt-001"
    row.severity = None
    row.created_at.isoformat.return_value = "2024-01-01T00:00:00+00:00"

    event = Event.from_system_event(row)

    # device.hold_changed has default_severity="info"
    assert event.severity == "info"


@pytest.mark.db
async def test_queue_event_for_session_carries_severity(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """queue_event_for_session forwards the severity kwarg to event_bus.publish."""
    from app.events.event_bus import queue_event_for_session

    captured: list[tuple[str, dict[str, Any], str | None]] = []

    async def fake_publish(event_type: str, data: dict[str, Any], severity: str | None = None) -> None:
        captured.append((event_type, data, severity))

    monkeypatch.setattr(event_bus, "publish", fake_publish)

    queue_event_for_session(
        db_session,
        "host.status_changed",
        {"host_id": "h1", "old_status": "online", "new_status": "offline"},
        severity="warning",
    )
    await db_session.commit()
    await event_bus.drain_handlers()

    assert len(captured) == 1
    event_type, data, severity = captured[0]
    assert event_type == "host.status_changed"
    assert data == {"host_id": "h1", "old_status": "online", "new_status": "offline"}
    assert severity == "warning"


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
    await asyncio.wait_for(started.wait(), 1)

    shutdown_task = asyncio.create_task(event_bus.shutdown())
    await asyncio.sleep(0)
    assert not shutdown_task.done()

    release.set()
    await asyncio.wait_for(shutdown_task, 1)

    assert completed.is_set()
