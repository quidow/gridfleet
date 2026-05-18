"""Prometheus metrics exposed by the hub event-bus subscriber."""

from __future__ import annotations

from app.grid.event_bus import (
    GRID_EVENT_BUS_CONNECTED,
    GRID_EVENT_BUS_DECODE_FAILURES_TOTAL,
    GRID_EVENT_BUS_EVENTS_RECEIVED_TOTAL,
    GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS,
)
from app.sessions.service_sync import SESSION_SYNC_WAKE_SOURCE_TOTAL


def test_event_bus_metrics_registered() -> None:
    assert GRID_EVENT_BUS_CONNECTED._name == "gridfleet_grid_event_bus_connected"
    assert GRID_EVENT_BUS_EVENTS_RECEIVED_TOTAL._name == "gridfleet_grid_event_bus_events_received"
    assert GRID_EVENT_BUS_DECODE_FAILURES_TOTAL._name == "gridfleet_grid_event_bus_decode_failures"
    assert GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS._name == "gridfleet_grid_event_bus_last_event_age_seconds"


def test_wake_source_counter_registered() -> None:
    assert SESSION_SYNC_WAKE_SOURCE_TOTAL._name == "gridfleet_session_sync_wake_source"
