"""Unit tests for pure-dict lifecycle_policy_state helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from app.devices.services.lifecycle_policy_state import (
    clear_deferred_stop,
    default_state,
    in_maintenance,
    parse_iso,
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


def test_set_deferred_stop_sets_pending_fields_without_action_stamp() -> None:
    state = default_state()
    set_deferred_stop(state, reason="probe failed")
    assert state["deferred_stop"] is True
    assert state["deferred_stop_reason"] == "probe failed"
    assert isinstance(state["deferred_stop_since"], str) and state["deferred_stop_since"]
    assert "last_action" not in state or state["last_action"] is None
    assert "last_action_at" not in state or state["last_action_at"] is None


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


def test_parse_iso_edges() -> None:
    assert parse_iso(None) is None
    assert parse_iso("") is None
    assert parse_iso("not a date") is None
    assert parse_iso("2026-05-07T01:02:03Z") is not None
