"""Unit tests for pure-dict lifecycle_policy_state helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.devices.services.lifecycle_policy_state import (
    clear_deferred_stop,
    default_state,
    in_maintenance,
    parse_iso,
    record_recovery_failed,
    record_recovery_recovered,
    record_recovery_started,
    set_deferred_stop,
)

if TYPE_CHECKING:
    from app.devices.models import Device


class _DeviceStub:
    def __init__(self, lifecycle_policy_state: dict[str, object] | None) -> None:
        self.lifecycle_policy_state = lifecycle_policy_state


def test_in_maintenance_reads_reason_with_defaults_merged() -> None:
    assert in_maintenance(cast("Device", _DeviceStub({"maintenance_reason": "operator"}))) is True
    assert in_maintenance(cast("Device", _DeviceStub({"maintenance_reason": None}))) is False
    assert in_maintenance(cast("Device", _DeviceStub({}))) is False
    assert in_maintenance(cast("Device", _DeviceStub(None))) is False


def test_set_deferred_stop_sets_pending_fields_and_action() -> None:
    state = default_state()
    set_deferred_stop(state, reason="probe failed")
    assert state["deferred_stop"] is True
    assert state["deferred_stop_reason"] == "probe failed"
    assert isinstance(state["deferred_stop_since"], str) and state["deferred_stop_since"]
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
    assert state["deferred_stop"] is False
    assert state["deferred_stop_reason"] is None
    assert state["deferred_stop_since"] is None
    # last_action is left untouched; callers that want to record auto_stop_cleared
    # must call set_action explicitly.
    assert state["last_action"] == "sentinel_action"
    assert state["last_action_at"] == "2000-01-01T00:00:00+00:00"


def test_record_recovery_started_stamps_action() -> None:
    state = default_state()
    record_recovery_started(state)
    assert state["last_action"] == "recovery_started"


def test_record_recovery_failed_records_source_reason_and_action() -> None:
    state = default_state()
    record_recovery_failed(
        state,
        source="probe",
        reason="boom",
    )
    assert state["last_failure_source"] == "probe"
    assert state["last_failure_reason"] == "boom"
    assert state["last_action"] == "recovery_failed"


def test_record_recovery_recovered_clears_backoff_and_stamps_action() -> None:
    state = default_state()
    state["backoff_until"] = "2026-05-07T01:02:03+00:00"
    state["recovery_backoff_attempts"] = 3
    record_recovery_recovered(state)
    assert state["backoff_until"] is None
    assert state["recovery_backoff_attempts"] == 0
    assert state["last_action"] == "auto_recovered"


def test_parse_iso_edges() -> None:
    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("not a date") is None
    assert parse_iso("2026-05-07T01:02:03Z") is not None
