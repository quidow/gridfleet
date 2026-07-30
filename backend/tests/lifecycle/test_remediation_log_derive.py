from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.devices.models import DeviceRemediationLogEntry
from app.lifecycle.services.remediation_log import (
    ACTION_AUTO_STOP_CLEARED,
    ACTION_AUTO_STOP_COMMISSIONED,
    ACTION_AUTO_STOP_DEFERRED,
    ACTION_AUTO_STOPPED,
    ACTION_RECOVERY_STARTED,
    ACTION_RESTART_COMMISSIONED,
    DIRECTIVE_START,
    DIRECTIVE_STOP,
    LadderState,
    build_policy_view,
    derive_ladder,
)


def _entry(
    *,
    kind: str,
    at: datetime,
    action: str,
    source: str = "node_health",
    reason: str | None = "failure",
    backoff_until: datetime | None = None,
    entry_id: uuid.UUID | None = None,
) -> DeviceRemediationLogEntry:
    return DeviceRemediationLogEntry(
        id=entry_id or uuid.uuid4(),
        device_id=uuid.uuid4(),
        kind=kind,
        source=source,
        action=action,
        reason=reason,
        backoff_until=backoff_until,
        at=at,
    )


def test_empty_log_is_unarmed_empty_ladder() -> None:
    ladder = derive_ladder([])

    assert ladder == LadderState(0, None, None, None, None, None)
    assert ladder.armed is False


def test_two_attempts_use_second_backoff_and_failure_trail() -> None:
    first_at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=1)
    second_deadline = second_at + timedelta(seconds=20)

    ladder = derive_ladder(
        [
            _entry(
                kind="attempt",
                at=first_at,
                action="recovery_failed",
                reason="first",
                backoff_until=first_at + timedelta(seconds=10),
            ),
            _entry(
                kind="attempt",
                at=second_at,
                action="recovery_failed",
                reason="second",
                backoff_until=second_deadline,
            ),
        ]
    )

    assert ladder.attempts == 2
    assert ladder.backoff_until == second_deadline
    assert ladder.last_failure_source == "node_health"
    assert ladder.last_failure_reason == "second"
    assert ladder.last_action == "recovery_failed"


def test_attempt_reset_attempt_only_counts_post_reset_attempt() -> None:
    first_at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    reset_at = first_at + timedelta(seconds=1)
    second_at = reset_at + timedelta(seconds=1)

    ladder = derive_ladder(
        [
            _entry(kind="attempt", at=first_at, action="recovery_failed", reason="old"),
            _entry(kind="reset", at=reset_at, action="self_healed"),
            _entry(kind="attempt", at=second_at, action="recovery_failed", reason="new"),
        ]
    )

    assert ladder.attempts == 1
    assert ladder.last_failure_reason == "new"


def test_reset_without_following_entries_clears_failure_and_attempts() -> None:
    first_at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    reset_at = first_at + timedelta(seconds=1)

    ladder = derive_ladder(
        [
            _entry(kind="attempt", at=first_at, action="recovery_failed"),
            _entry(kind="reset", at=reset_at, action="self_healed"),
        ]
    )

    assert ladder.attempts == 0
    assert ladder.last_failure_source is None
    assert ladder.last_failure_reason is None
    assert ladder.last_action == "self_healed"
    assert ladder.last_action_at == reset_at


def test_failure_and_action_rows_only_update_their_own_trails() -> None:
    base = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    ladder = derive_ladder(
        [
            _entry(kind="failure", at=base, action="failure_observed", reason="observed"),
            _entry(kind="action", at=base + timedelta(seconds=1), action="recovery_started"),
        ]
    )

    assert ladder.attempts == 0
    assert ladder.last_failure_reason == "observed"
    assert ladder.last_action == "recovery_started"


def test_same_timestamp_rows_are_ordered_by_id() -> None:
    at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)

    ladder = derive_ladder(
        [
            _entry(kind="reset", at=at, action="self_healed", entry_id=uuid.UUID(int=1)),
            _entry(
                kind="failure",
                at=at,
                action="failure_observed",
                reason="after reset",
                entry_id=uuid.UUID(int=2),
            ),
        ]
    )

    assert ladder.last_failure_reason == "after reset"


def test_backoff_active_returns_only_future_deadline() -> None:
    now = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    future = now + timedelta(seconds=1)

    assert LadderState(1, future, None, None, None, None).backoff_active(now=now) == future
    assert LadderState(1, now, None, None, None, None).backoff_active(now=now) is None


def test_build_policy_view_derives_retired_keys_and_serializes_datetimes() -> None:
    at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    deadline = at + timedelta(seconds=10)
    ladder = LadderState(2, deadline, "node_health", "failed", "recovery_failed", at)

    view = build_policy_view(
        ladder,
        {
            "maintenance_reason": "operator",
            "deferred_stop": True,
            "deferred_stop_reason": "busy",
            "deferred_stop_since": at.isoformat(),
        },
    )

    assert set(view) == {
        "maintenance_reason",
        "deferred_stop",
        "deferred_stop_reason",
        "deferred_stop_since",
        "backoff_until",
        "recovery_backoff_attempts",
        "last_failure_source",
        "last_failure_reason",
        "last_action",
        "last_action_at",
    }
    assert view["backoff_until"] == deadline.isoformat()
    assert view["last_action_at"] == at.isoformat()
    assert view["deferred_stop"] is False
    assert view["deferred_stop_reason"] is None
    assert view["deferred_stop_since"] is None
    assert build_policy_view(LadderState(0, None, None, None, None, None), None)["deferred_stop"] is False


def test_empty_log_has_no_directive_or_deferred_stop_and_is_inactive() -> None:
    ladder = derive_ladder([])

    assert ladder.node_directive is None
    assert ladder.deferred_stop_pending is False
    assert ladder.episode_active is False


def test_auto_stop_commissioned_derives_stop_directive() -> None:
    ladder = derive_ladder(
        [
            _entry(
                kind="action",
                at=datetime(2026, 7, 12, 12, 0, tzinfo=UTC),
                action=ACTION_AUTO_STOP_COMMISSIONED,
                reason="node crashed",
            )
        ]
    )

    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_STOP
    assert ladder.node_directive.reason == "node crashed"
    assert ladder.episode_active is True


def test_newest_directive_wins_and_recovery_start_has_no_watermark() -> None:
    base = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder(
        [
            _entry(kind="action", at=base, action=ACTION_AUTO_STOP_COMMISSIONED),
            _entry(kind="action", at=base + timedelta(seconds=1), action=ACTION_RECOVERY_STARTED),
        ]
    )

    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_START
    assert ladder.node_directive.restart_watermark is None


def test_restart_commission_derives_start_watermark() -> None:
    at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder([_entry(kind="action", at=at, action=ACTION_RESTART_COMMISSIONED)])

    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_START
    assert ladder.node_directive.restart_watermark == at


def test_restart_watermark_survives_a_newer_plain_start() -> None:
    base = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder(
        [
            _entry(kind="action", at=base, action=ACTION_RESTART_COMMISSIONED),
            _entry(kind="action", at=base + timedelta(seconds=1), action=ACTION_RECOVERY_STARTED),
        ]
    )

    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_START
    assert ladder.node_directive.restart_watermark == base


def test_failed_recovery_replaces_start_with_stop_directive() -> None:
    base = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder(
        [
            _entry(kind="action", at=base, action=ACTION_RECOVERY_STARTED),
            _entry(kind="action", at=base + timedelta(seconds=1), action=ACTION_AUTO_STOP_COMMISSIONED),
        ]
    )

    assert ladder.node_directive is not None
    assert ladder.node_directive.kind == DIRECTIVE_STOP


def test_reset_supersedes_directive() -> None:
    base = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder(
        [
            _entry(kind="action", at=base, action=ACTION_AUTO_STOP_COMMISSIONED),
            _entry(kind="reset", at=base + timedelta(seconds=1), action="self_healed"),
        ]
    )

    assert ladder.node_directive is None


def test_deferred_stop_derives_pending_reason_and_since() -> None:
    at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder([_entry(kind="action", at=at, action=ACTION_AUTO_STOP_DEFERRED, reason="probe failed")])

    assert ladder.deferred_stop_pending is True
    assert ladder.deferred_stop_reason == "probe failed"
    assert ladder.deferred_stop_since == at


def test_deferred_stop_completion_and_reset_clear_pending() -> None:
    base = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    for action, kind in (
        (ACTION_AUTO_STOPPED, "action"),
        (ACTION_AUTO_STOP_CLEARED, "action"),
        ("reset", "reset"),
    ):
        ladder = derive_ladder(
            [
                _entry(kind="action", at=base, action=ACTION_AUTO_STOP_DEFERRED),
                _entry(kind=kind, at=base + timedelta(seconds=1), action=action),
            ]
        )
        assert ladder.deferred_stop_pending is False


def test_non_directive_actions_do_not_derive_node_directive() -> None:
    at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    ladder = derive_ladder(
        [
            _entry(kind="action", at=at, action=ACTION_AUTO_STOPPED),
            _entry(kind="action", at=at + timedelta(seconds=1), action="node_monitor_recovered"),
            _entry(kind="action", at=at + timedelta(seconds=2), action="self_healed"),
        ]
    )

    assert ladder.node_directive is None


def test_episode_active_truth_table() -> None:
    at = datetime(2026, 7, 12, 12, 0, tzinfo=UTC)
    cases = [
        ([_entry(kind="attempt", at=at, action="recovery_failed")], True),
        ([_entry(kind="failure", at=at, action="failure_observed")], True),
        ([_entry(kind="action", at=at, action=ACTION_AUTO_STOP_COMMISSIONED)], True),
        ([_entry(kind="action", at=at, action=ACTION_AUTO_STOP_DEFERRED)], True),
        (
            [
                _entry(kind="action", at=at, action=ACTION_AUTO_STOP_COMMISSIONED),
                _entry(kind="reset", at=at + timedelta(seconds=1), action="self_healed"),
            ],
            False,
        ),
    ]
    for entries, expected in cases:
        assert derive_ladder(entries).episode_active is expected
