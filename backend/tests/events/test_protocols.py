"""Verify event domain protocols."""

from __future__ import annotations

from app.events.event_bus import EventBus
from app.events.protocols import EventPublisher


def test_event_bus_satisfies_publisher_protocol() -> None:
    bus = EventBus()
    assert isinstance(bus, EventPublisher)
