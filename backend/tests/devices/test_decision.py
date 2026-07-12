"""Truth table for the explicit decision ladders.

Every row mirrors a decision the old priority arbiter (intent_evaluator.py,
deleted in this plan) made for the same inputs. This table is the
behavior-preservation contract for the representation swap.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.devices.services.decision import (
    Command,
    CommandKind,
    DecisionFacts,
    decide_grid_routing,
    decide_node_process,
    decide_recovery,
    parse_command,
)
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON
from app.lifecycle.services.remediation_log import DIRECTIVE_START, DIRECTIVE_STOP, NodeDirective

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
DEVICE = uuid.uuid4()
RUN = uuid.uuid4()


def cmd(kind: CommandKind, **kw: object) -> Command:
    return Command(
        kind=kind,
        source=kw.pop("source", f"{kind.value}:{DEVICE}"),
        run_id=kw.pop("run_id", None),
        restart_requested_at=kw.pop("restart_requested_at", None),
        reason_detail=kw.pop("reason_detail", None),
    )


def facts(**kw: object) -> DecisionFacts:
    defaults = dict(
        in_maintenance=False,
        device_checks_unhealthy=False,
        in_service=True,
        reservation_run_id=None,
        cooldown_active=False,
        cooldown_reason=None,
        remediation_directive=None,
    )
    defaults.update(kw)
    return DecisionFacts(**defaults)


def directive(kind: str, *, reason: str | None = None, watermark: datetime | None = None) -> NodeDirective:
    return NodeDirective(kind=kind, reason=reason, restart_watermark=watermark)


# --- node_process ladder ---


def test_no_commands_out_of_service_stops() -> None:
    d = decide_node_process([], facts(in_service=False))
    assert (d.desired_state, d.stop_mode) == ("stopped", None)


def test_no_commands_in_service_baselines_running() -> None:
    d = decide_node_process([], facts())
    assert d.desired_state == "running"
    assert "baseline:idle" in d.reason


def test_operator_stop_beats_operator_start() -> None:
    d = decide_node_process([cmd(CommandKind.operator_stop), cmd(CommandKind.operator_start)], facts())
    assert (d.desired_state, d.stop_mode) == ("stopped", "hard")


def test_forced_release_beats_operator_start() -> None:
    d = decide_node_process([cmd(CommandKind.forced_release), cmd(CommandKind.operator_start)], facts())
    assert (d.desired_state, d.stop_mode) == ("stopped", "hard")


def test_maintenance_fact_beats_derived_recovery_start() -> None:
    d = decide_node_process([], facts(in_maintenance=True, remediation_directive=directive(DIRECTIVE_START)))
    assert (d.desired_state, d.stop_mode) == ("stopping_graceful", "graceful")


def test_maintenance_fact_suppresses_baseline() -> None:
    d = decide_node_process([], facts(in_maintenance=True))
    assert (d.desired_state, d.stop_mode) == ("stopping_graceful", "graceful")


def test_connectivity_park_defers_without_start_command() -> None:
    d = decide_node_process([], facts(device_checks_unhealthy=True))
    assert (d.desired_state, d.stop_mode) == ("running_blocked", "defer")


def test_connectivity_park_suppressed_by_active_start_command() -> None:
    # Semantic delta #3 of the synthesis migration: an active start command
    # overrides the connectivity park (no revoke ritual needed).
    d = decide_node_process([cmd(CommandKind.operator_start)], facts(device_checks_unhealthy=True))
    assert d.desired_state == "running"


def test_newest_watermark_wins_among_starts() -> None:
    older = NOW - timedelta(seconds=60)
    newer = NOW - timedelta(seconds=5)
    d = decide_node_process(
        [
            cmd(CommandKind.operator_start, restart_requested_at=older),
        ],
        facts(remediation_directive=directive(DIRECTIVE_START, watermark=newer)),
    )
    assert d.desired_state == "running"
    assert d.restart_requested_at == newer


def test_watermark_start_wins_over_plain_start() -> None:
    watermark = NOW - timedelta(seconds=5)
    d = decide_node_process(
        [cmd(CommandKind.operator_start)],
        facts(remediation_directive=directive(DIRECTIVE_START, watermark=watermark)),
    )
    assert d.desired_state == "running"
    assert d.restart_requested_at == watermark


def test_plain_starts_carry_no_watermark() -> None:
    d = decide_node_process([cmd(CommandKind.operator_start)], facts())
    assert d.desired_state == "running"
    assert d.restart_requested_at is None


def test_derived_stop_directive_gracefully_stops() -> None:
    d = decide_node_process([], facts(remediation_directive=directive(DIRECTIVE_STOP, reason="node crashed")))

    assert (d.desired_state, d.stop_mode, d.reason) == ("stopping_graceful", "graceful", "node crashed")


def test_derived_stop_beats_connectivity_park() -> None:
    d = decide_node_process(
        [], facts(device_checks_unhealthy=True, remediation_directive=directive(DIRECTIVE_STOP, reason="crashed"))
    )

    assert d.desired_state == "stopping_graceful"


def test_maintenance_beats_derived_stop() -> None:
    d = decide_node_process(
        [], facts(in_maintenance=True, remediation_directive=directive(DIRECTIVE_STOP, reason="crashed"))
    )

    assert d.reason == "maintenance hold"


def test_operator_stop_beats_derived_start() -> None:
    d = decide_node_process([cmd(CommandKind.operator_stop)], facts(remediation_directive=directive(DIRECTIVE_START)))

    assert (d.desired_state, d.stop_mode) == ("stopped", "hard")


def test_verification_start_suppresses_derived_stop() -> None:
    """A verification lease structurally replaces the retired revoke-before-register ritual."""
    d = decide_node_process(
        [cmd(CommandKind.verification_start)], facts(remediation_directive=directive(DIRECTIVE_STOP, reason="crashed"))
    )

    assert d.desired_state == "running"


def test_operator_start_suppresses_derived_stop() -> None:
    d = decide_node_process(
        [cmd(CommandKind.operator_start)], facts(remediation_directive=directive(DIRECTIVE_STOP, reason="crashed"))
    )

    assert d.desired_state == "running"


def test_derived_start_suppresses_derived_stop_history() -> None:
    d = decide_node_process([], facts(remediation_directive=directive(DIRECTIVE_START)))

    assert d.desired_state == "running"


def test_derived_start_suppresses_connectivity_park() -> None:
    d = decide_node_process([], facts(device_checks_unhealthy=True, remediation_directive=directive(DIRECTIVE_START)))

    assert d.desired_state == "running"


def test_derived_start_carries_restart_watermark() -> None:
    watermark = NOW - timedelta(seconds=5)
    d = decide_node_process([], facts(remediation_directive=directive(DIRECTIVE_START, watermark=watermark)))

    assert d.desired_state == "running"
    assert d.restart_requested_at == watermark


def test_derived_watermark_folds_with_stored_watermarks_newest_wins() -> None:
    older = NOW - timedelta(seconds=10)
    newer = NOW - timedelta(seconds=5)
    first = decide_node_process(
        [cmd(CommandKind.operator_start, restart_requested_at=older)],
        facts(remediation_directive=directive(DIRECTIVE_START, watermark=newer)),
    )
    second = decide_node_process(
        [cmd(CommandKind.operator_start, restart_requested_at=newer)],
        facts(remediation_directive=directive(DIRECTIVE_START, watermark=older)),
    )

    assert first.restart_requested_at == newer
    assert second.restart_requested_at == newer


def test_derived_start_requires_in_service() -> None:
    d = decide_node_process([], facts(in_service=False, remediation_directive=directive(DIRECTIVE_START)))

    assert (d.desired_state, d.stop_mode) == ("stopped", None)


def test_derived_stop_holds_out_of_service_too() -> None:
    d = decide_node_process(
        [], facts(in_service=False, remediation_directive=directive(DIRECTIVE_STOP, reason="crashed"))
    )

    assert (d.desired_state, d.stop_mode) == ("stopping_graceful", "graceful")


# --- grid_routing ladder ---


def test_grid_no_reservation_accepts() -> None:
    d = decide_grid_routing(facts())
    assert (d.run_id, d.accepting_new_sessions) == (None, True)


def test_grid_active_reservation_routes_run() -> None:
    d = decide_grid_routing(facts(reservation_run_id=RUN))
    assert (d.run_id, d.accepting_new_sessions) == (RUN, True)


def test_grid_cooldown_keeps_run_but_blocks_sessions() -> None:
    d = decide_grid_routing(facts(reservation_run_id=RUN, cooldown_active=True))
    assert (d.run_id, d.accepting_new_sessions) == (RUN, False)


# --- recovery ladder ---


def test_recovery_default_allows() -> None:
    d = decide_recovery([], facts())
    assert (d.allowed, d.reason) == (True, None)


def test_operator_recovery_deny_beats_default_allow() -> None:
    d = decide_recovery(
        [cmd(CommandKind.operator_recovery_deny, reason_detail="Operator stopped the node")],
        facts(),
    )
    assert d.allowed is False
    assert d.reason == "Operator stopped the node"


def test_maintenance_denies_recovery_with_exact_constant() -> None:
    # exit_maintenance matches this string to clear the suppression — any
    # drift freezes effective_state at "blocked" (see intent_synthesis history).
    d = decide_recovery([], facts(in_maintenance=True))
    assert d.allowed is False
    assert d.reason == MAINTENANCE_HOLD_SUPPRESSION_REASON


def test_cooldown_denies_recovery_with_exclusion_reason() -> None:
    d = decide_recovery(
        [],
        facts(reservation_run_id=RUN, cooldown_active=True, cooldown_reason="run failure cooldown"),
    )
    assert d.allowed is False
    assert d.reason == "run failure cooldown"


def test_recovery_default_allow_has_no_reason_or_source() -> None:
    d = decide_recovery([], facts())

    assert (d.allowed, d.reason, d.source) == (True, None, None)


# --- parsing ---


def test_parse_known_kind_and_ttl() -> None:
    from app.devices.models import DeviceIntent

    row = DeviceIntent(
        device_id=DEVICE,
        source=f"operator:stop:node:{DEVICE}",
        kind=CommandKind.operator_stop.value,
        payload={"action": "stop", "priority": 100, "stop_mode": "hard"},  # legacy keys ignored
        expires_at=None,
    )
    parsed = parse_command(row, NOW)
    assert parsed is not None and parsed.kind is CommandKind.operator_stop


def test_parse_expired_row_returns_none() -> None:
    from app.devices.models import DeviceIntent

    row = DeviceIntent(
        device_id=DEVICE,
        source=f"operator:start:{DEVICE}",
        kind=CommandKind.operator_start.value,
        payload={},
        expires_at=NOW - timedelta(seconds=1),
    )
    assert parse_command(row, NOW) is None


def test_parse_unknown_kind_returns_none() -> None:
    from app.devices.models import DeviceIntent

    row = DeviceIntent(device_id=DEVICE, source="mystery:thing", kind="mystery", payload={})
    assert parse_command(row, NOW) is None
