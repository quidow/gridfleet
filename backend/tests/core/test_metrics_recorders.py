"""Tests for Phase 3 desired-state Prometheus counters."""

from __future__ import annotations

from app.core import metrics_recorders


def test_appium_desired_state_writes_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_DESIRED_STATE_WRITES
    assert counter._name == "appium_desired_state_writes"
    assert sorted(counter._labelnames) == ["caller", "target_state"]


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


def test_appium_reconciler_start_failures_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_START_FAILURES
    assert counter._name == "appium_reconciler_start_failures"
    assert sorted(counter._labelnames) == ["reason"]


def test_appium_reconciler_stop_failures_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_RECONCILER_STOP_FAILURES
    assert counter._name == "appium_reconciler_stop_failures"
    assert sorted(counter._labelnames) == ["reason"]


def test_appium_terminate_failed_counter_exists() -> None:
    counter = metrics_recorders.APPIUM_TERMINATE_FAILED_TOTAL
    assert counter._name == "appium_terminate_failed"
    assert counter._labelnames == ()


def test_forced_release_node_stop_counter_exists() -> None:
    counter = metrics_recorders.FORCED_RELEASE_NODE_STOP_TOTAL
    assert counter._name == "gridfleet_forced_release_node_stop"
    assert counter._labelnames == ()
