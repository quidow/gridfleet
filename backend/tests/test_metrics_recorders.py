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


def test_appium_reconciler_convergence_actions_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_CONVERGENCE_ACTIONS
    assert counter._name == "appium_reconciler_convergence_actions"
    assert sorted(counter._labelnames) == ["action"]


def test_appium_reconciler_host_cycle_seconds_histogram_exists() -> None:
    histogram = metrics_recorders.APPIUM_RECONCILER_HOST_CYCLE_SECONDS
    assert histogram._name == "appium_reconciler_host_cycle_seconds"
    assert sorted(histogram._labelnames) == ["host_id"]


def test_appium_reconciler_allocation_collisions_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_ALLOCATION_COLLISIONS
    assert counter._name == "appium_reconciler_allocation_collisions"
    assert counter._labelnames == ()


def test_appium_reconciler_transition_token_expired_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED
    assert counter._name == "appium_reconciler_transition_token_expired"
    assert counter._labelnames == ()


def test_appium_reconciler_start_failures_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_START_FAILURES
    assert counter._name == "appium_reconciler_start_failures"
    assert sorted(counter._labelnames) == ["reason"]


def test_appium_reconciler_stop_failures_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_STOP_FAILURES
    assert counter._name == "appium_reconciler_stop_failures"
    assert sorted(counter._labelnames) == ["reason"]
