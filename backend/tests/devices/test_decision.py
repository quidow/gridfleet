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

NOW = datetime(2026, 7, 9, 12, 0, 0, tzinfo=UTC)
DEVICE = uuid.uuid4()
RUN = uuid.uuid4()


def cmd(kind: CommandKind, **kw: object) -> Command:
    return Command(
        kind=kind,
        source=kw.pop("source", f"{kind.value}:{DEVICE}"),
        run_id=kw.pop("run_id", None),
        transition_token=kw.pop("transition_token", None),
        transition_deadline=kw.pop("transition_deadline", None),
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
    )
    defaults.update(kw)
    return DecisionFacts(**defaults)


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


def test_maintenance_fact_beats_auto_recovery_start() -> None:
    d = decide_node_process([cmd(CommandKind.auto_recovery_start)], facts(in_maintenance=True))
    assert (d.desired_state, d.stop_mode) == ("stopping_graceful", "graceful")


def test_maintenance_fact_suppresses_baseline() -> None:
    d = decide_node_process([], facts(in_maintenance=True))
    assert (d.desired_state, d.stop_mode) == ("stopping_graceful", "graceful")


def test_health_failure_stop_beats_verification_start() -> None:
    # Why the verification path revokes failure_stop_sources before starting.
    d = decide_node_process([cmd(CommandKind.health_failure_stop), cmd(CommandKind.verification_start)], facts())
    assert (d.desired_state, d.stop_mode) == ("stopping_graceful", "graceful")


def test_connectivity_park_defers_without_start_command() -> None:
    d = decide_node_process([], facts(device_checks_unhealthy=True))
    assert (d.desired_state, d.stop_mode) == ("running_blocked", "defer")


def test_connectivity_park_suppressed_by_active_start_command() -> None:
    # Semantic delta #3 of the synthesis migration: an active start command
    # overrides the connectivity park (no revoke ritual needed).
    d = decide_node_process([cmd(CommandKind.operator_start)], facts(device_checks_unhealthy=True))
    assert d.desired_state == "running"


def test_token_bearing_start_wins_over_tokenless_start() -> None:
    token = uuid.uuid4()
    deadline = NOW + timedelta(seconds=120)
    d = decide_node_process(
        [
            cmd(CommandKind.operator_start, source=f"operator:start:{DEVICE}"),
            cmd(
                CommandKind.auto_recovery_start,
                source=f"auto_recovery:node:{DEVICE}",
                transition_token=token,
                transition_deadline=deadline,
            ),
        ],
        facts(),
    )
    assert d.desired_state == "running"
    assert d.transition_token == token
    assert d.transition_deadline == deadline


def test_token_without_deadline_is_dropped() -> None:
    d = decide_node_process([cmd(CommandKind.operator_start, transition_token=uuid.uuid4())], facts())
    assert d.desired_state == "running"
    assert d.transition_token is None


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


def test_operator_recovery_deny_beats_auto_allow() -> None:
    d = decide_recovery(
        [
            cmd(CommandKind.operator_recovery_deny, reason_detail="Operator stopped the node"),
            cmd(CommandKind.auto_recovery_allow, reason_detail="recovering"),
        ],
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
        [cmd(CommandKind.auto_recovery_allow)],
        facts(reservation_run_id=RUN, cooldown_active=True, cooldown_reason="run failure cooldown"),
    )
    assert d.allowed is False
    assert d.reason == "run failure cooldown"


def test_auto_recovery_allow_threads_reason_and_source() -> None:
    d = decide_recovery(
        [
            cmd(
                CommandKind.auto_recovery_allow,
                source=f"auto_recovery:recovery:{DEVICE}",
                reason_detail="Node health restart",
            )
        ],
        facts(),
    )
    assert d.allowed is True
    assert d.reason == "Node health restart"
    assert d.source == f"auto_recovery:recovery:{DEVICE}"


# --- parsing ---


def test_parse_known_sources_and_ttl() -> None:
    from app.devices.models import DeviceIntent

    row = DeviceIntent(
        device_id=DEVICE,
        source=f"operator:stop:node:{DEVICE}",
        axis="node_process",
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
        axis="node_process",
        payload={},
        expires_at=NOW - timedelta(seconds=1),
    )
    assert parse_command(row, NOW) is None


def test_parse_unknown_source_returns_none() -> None:
    from app.devices.models import DeviceIntent

    row = DeviceIntent(device_id=DEVICE, source="mystery:thing", axis="node_process", payload={})
    assert parse_command(row, NOW) is None
