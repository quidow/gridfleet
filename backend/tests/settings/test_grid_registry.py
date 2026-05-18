"""Registry entries covering the grid event-bus subscriber."""

from __future__ import annotations

from app.settings.registry import SETTINGS_REGISTRY


def test_session_poll_interval_default_is_30() -> None:
    defn = SETTINGS_REGISTRY["grid.session_poll_interval_sec"]
    assert defn.default == 30, (
        "Subscriber upgrade downgrades the poll to a 30s drift reconciler; "
        "see .superpowers/specs/2026-05-18-grid-stability-perf-design.md"
    )


def test_event_bus_subscribe_url_registered() -> None:
    defn = SETTINGS_REGISTRY["grid.event_bus_subscribe_url"]
    assert defn.category == "grid"
    assert defn.setting_type == "string"
    assert defn.default == "tcp://selenium-hub:4442"
    assert defn.env_var == "GRIDFLEET_GRID_EVENT_BUS_SUBSCRIBE_URL"


def test_event_bus_publish_url_registered() -> None:
    defn = SETTINGS_REGISTRY["grid.event_bus_publish_url"]
    assert defn.category == "grid"
    assert defn.setting_type == "string"
    assert defn.default == "tcp://selenium-hub:4443"
    assert defn.env_var == "GRIDFLEET_GRID_EVENT_BUS_PUBLISH_URL"
