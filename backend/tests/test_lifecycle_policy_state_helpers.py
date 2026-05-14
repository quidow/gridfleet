"""Unit tests for pure-dict lifecycle_policy_state helpers."""

from __future__ import annotations

from app.devices.services.lifecycle_policy_state import (
    MAINTENANCE_HOLD_SUPPRESSION_REASON,
    clear_deferred_stop,
    default_state,
    parse_iso,
    record_backoff_suppressed,
    record_manual_recovered,
    record_recovery_failed,
    record_recovery_recovered,
    record_recovery_started,
    set_deferred_stop,
)


def test_set_deferred_stop_sets_pending_fields_and_action() -> None:
    state = default_state()
    set_deferred_stop(state, reason="probe failed")
    assert state["stop_pending"] is True
    assert state["stop_pending_reason"] == "probe failed"
    assert isinstance(state["stop_pending_since"], str) and state["stop_pending_since"]
    assert state["last_action"] == "auto_stop_deferred"
    assert isinstance(state["last_action_at"], str) and state["last_action_at"]


def test_clear_deferred_stop_resets_pending_fields_only() -> None:
    state = default_state()
    set_deferred_stop(state, reason="probe failed")
    # Sentinel last_action so the assertion below catches an accidental
    # re-stamp by clear_deferred_stop, even when the new value happens to
    # match the prior set_deferred_stop action string.
    state["last_action"] = "sentinel_action"
    state["last_action_at"] = "2000-01-01T00:00:00+00:00"
    clear_deferred_stop(state)
    assert state["stop_pending"] is False
    assert state["stop_pending_reason"] is None
    assert state["stop_pending_since"] is None
    # last_action is left untouched; callers that want to record auto_stop_cleared
    # must call set_action explicitly.
    assert state["last_action"] == "sentinel_action"
    assert state["last_action_at"] == "2000-01-01T00:00:00+00:00"


def test_record_recovery_started_clears_suppression_and_stamps_action() -> None:
    state = default_state()
    state["recovery_suppressed_reason"] = "leftover from previous attempt"
    record_recovery_started(state)
    assert state["recovery_suppressed_reason"] is None
    assert state["last_action"] == "recovery_started"


def test_record_recovery_failed_records_source_reason_and_action() -> None:
    state = default_state()
    record_recovery_failed(
        state,
        source="probe",
        reason="boom",
        suppression_reason="Automatic restart failed",
    )
    assert state["last_failure_source"] == "probe"
    assert state["last_failure_reason"] == "boom"
    assert state["recovery_suppressed_reason"] == "Automatic restart failed"
    assert state["last_action"] == "recovery_failed"


def test_record_backoff_suppressed_uses_provided_until_iso() -> None:
    state = default_state()
    record_backoff_suppressed(state, until_iso="2026-05-07T01:02:03+00:00")
    assert state["recovery_suppressed_reason"] == "Backing off until 2026-05-07T01:02:03+00:00"
    assert state["last_action"] == "recovery_suppressed"


def test_record_recovery_recovered_clears_backoff_and_stamps_action() -> None:
    state = default_state()
    state["backoff_until"] = "2026-05-07T01:02:03+00:00"
    state["recovery_backoff_attempts"] = 3
    state["recovery_suppressed_reason"] = "Backing off"
    record_recovery_recovered(state)
    assert state["backoff_until"] is None
    assert state["recovery_backoff_attempts"] == 0
    assert state["recovery_suppressed_reason"] is None
    assert state["last_action"] == "auto_recovered"


def test_parse_iso_and_manual_recovered_edges() -> None:
    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("not a date") is None
    assert parse_iso("2026-05-07T01:02:03Z") is not None

    state = default_state()
    state["last_failure_source"] = "node_health"
    state["last_failure_reason"] = "boom"
    state["recovery_suppressed_reason"] = MAINTENANCE_HOLD_SUPPRESSION_REASON
    state["backoff_until"] = "2026-05-07T01:02:03+00:00"
    state["recovery_backoff_attempts"] = 2

    record_manual_recovered(state)

    assert state["last_failure_source"] is None
    assert state["last_failure_reason"] is None
    assert state["recovery_suppressed_reason"] is None
    assert state["backoff_until"] is None
    assert state["recovery_backoff_attempts"] == 0
    assert state["last_action"] == "manual_recovered"
