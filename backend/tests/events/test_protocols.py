"""Verify event domain protocols."""

from __future__ import annotations

from typing import Any

from app.events.event_bus import EventBus
from app.events.protocols import EventPublisher, EventReader, EventSubscriber


def test_event_bus_satisfies_publisher_protocol() -> None:
    bus = EventBus()
    assert isinstance(bus, EventPublisher)


def test_event_bus_satisfies_subscriber_protocol() -> None:
    bus = EventBus()
    assert isinstance(bus, EventSubscriber)


class _FakeReader:
    async def get_recent_events_persisted(
        self,
        *,
        limit: int = 25,
        offset: int = 0,
        event_types: list[str] | None = None,
        severities: list[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        return [], 0


def test_fake_reader_satisfies_protocol() -> None:
    reader: EventReader = _FakeReader()
    assert reader is not None
