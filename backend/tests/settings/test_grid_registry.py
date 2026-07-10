"""Registry entries covering the grid allocation/session-sync knobs."""

from __future__ import annotations

from app.sessions.appium_sweep import SESSION_POLL_INTERVAL_SEC
from app.settings.registry import SETTINGS_REGISTRY


def test_session_poll_interval_constant_is_30() -> None:
    # Subscriber upgrade downgraded the poll to a 30s drift reconciler
    # (see .superpowers/specs/2026-05-18-grid-stability-perf-design.md);
    # WS-4.2 fixed the cadence as a plumbing constant.
    assert SESSION_POLL_INTERVAL_SEC == 30.0


def test_claim_window_default_exceeds_appium_create_time() -> None:
    defn = SETTINGS_REGISTRY["grid.claim_window_sec"]
    assert defn.default == 120, "Must exceed worst-case Appium session-creation time so in-flight creates aren't reaped"
    assert defn.max_value == 600
