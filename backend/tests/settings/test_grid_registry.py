"""Registry entries covering the grid allocation/session-sync knobs."""

from __future__ import annotations

from app.settings.registry import SETTINGS_REGISTRY


def test_session_poll_interval_default_is_30() -> None:
    defn = SETTINGS_REGISTRY["grid.session_poll_interval_sec"]
    assert defn.default == 30, (
        "Subscriber upgrade downgrades the poll to a 30s drift reconciler; "
        "see .superpowers/specs/2026-05-18-grid-stability-perf-design.md"
    )


def test_claim_window_default_exceeds_appium_create_time() -> None:
    defn = SETTINGS_REGISTRY["grid.claim_window_sec"]
    assert defn.default == 120, "Must exceed worst-case Appium session-creation time so in-flight creates aren't reaped"
    assert defn.max_value == 600
