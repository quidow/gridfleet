"""Tests for Phase 3 desired-state Prometheus counters."""

from __future__ import annotations

from app import metrics_recorders


def test_appium_desired_state_writes_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_DESIRED_STATE_WRITES
    assert counter._name == "appium_desired_state_writes"
    assert sorted(counter._labelnames) == ["caller", "target_state"]


def test_appium_transition_token_writes_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_TRANSITION_TOKEN_WRITES
    assert counter._name == "appium_transition_token_writes"
    assert sorted(counter._labelnames) == ["caller"]


def test_appium_transition_token_overridden_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_TRANSITION_TOKEN_OVERRIDDEN
    assert counter._name == "appium_transition_token_overridden"
    assert sorted(counter._labelnames) == ["losing_source", "winning_source"]
