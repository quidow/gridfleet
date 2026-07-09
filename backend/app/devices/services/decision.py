"""Explicit decision ladders: desired state = f(stored commands, facts).

Replaces the priority arbiter (intent_evaluator.py) and fact-intent synthesis
(intent_synthesis.py). Stored ``device_intents`` rows are genuine commands and
leases only; facts (reservation, maintenance, connectivity, service
eligibility) are read directly and folded in here. Precedence is the ordered
code below — it mirrors the retired numeric ladder exactly
(operator stop 100 > forced release 95 > maintenance 80 > health-failure 60 >
connectivity park 50 > starts 20 > baseline 10).

Pure module: no DB access. ``gather_decision_facts`` lives in
intent_reconciler.py; ``tests/devices/test_decision.py`` is the
behavior-preservation truth table.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from app.appium_nodes.models import AppiumDesiredState
from app.core.observability import get_logger
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON

if TYPE_CHECKING:
    from app.devices.models import DeviceIntent

logger = get_logger(__name__)

NodeProcessState = str  # "running" | "running_blocked" | "stopping_graceful" | "stopped"
StopMode = str  # "hard" | "graceful" | "defer"


class CommandKind(StrEnum):
    operator_stop = "operator:stop:node"
    operator_recovery_deny = "operator:stop:recovery"
    forced_release = "forced_release"
    health_failure_stop = "health_failure:node"
    operator_start = "operator:start"
    verification_start = "verification"
    auto_recovery_start = "auto_recovery:node"
    auto_recovery_allow = "auto_recovery:recovery"


_START_KINDS = frozenset({CommandKind.operator_start, CommandKind.verification_start, CommandKind.auto_recovery_start})
# Longest prefix first so operator:stop:recovery is not shadowed by operator:stop:node.
_PREFIX_ORDER = sorted(CommandKind, key=lambda kind: len(kind.value), reverse=True)


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    source: str
    run_id: uuid.UUID | None
    transition_token: uuid.UUID | None
    transition_deadline: datetime | None
    reason_detail: str | None


@dataclass(frozen=True)
class DecisionFacts:
    in_maintenance: bool
    device_checks_unhealthy: bool  # device_checks_healthy IS FALSE
    in_service: bool  # device_in_service(device): baseline eligibility (F-G1)
    reservation_run_id: uuid.UUID | None  # None when unreserved OR indefinitely excluded
    cooldown_active: bool  # excluded AND excluded_until > now
    cooldown_reason: str | None


@dataclass(frozen=True)
class NodeProcessDecision:
    desired_state: NodeProcessState
    stop_mode: StopMode | None
    reason: str
    transition_token: uuid.UUID | None = None
    transition_deadline: datetime | None = None


@dataclass(frozen=True)
class GridRoutingDecision:
    run_id: uuid.UUID | None
    accepting_new_sessions: bool
    reason: str


@dataclass(frozen=True)
class RecoveryDecision:
    allowed: bool
    reason: str | None
    source: str | None


def parse_command(intent: DeviceIntent, now: datetime) -> Command | None:
    """Parse a stored row into a typed command; None for expired or unknown rows.

    Unknown sources are logged and ignored (the retired arbiter ranked them at
    priority 0, where they could never win either).
    """
    if intent.expires_at is not None and intent.expires_at <= now:
        return None
    kind = next((k for k in _PREFIX_ORDER if intent.source.startswith(k.value)), None)
    if kind is None:
        logger.warning("device_intent_unknown_source", source=intent.source, device_id=str(intent.device_id))
        return None
    token = _optional_uuid(intent.payload.get("transition_token"))
    deadline = _optional_datetime(intent.payload.get("transition_deadline"))
    if token is not None and deadline is None:
        token = None  # a token without a deadline can never be cleared — drop it
    reason_detail = intent.payload.get("reason")
    return Command(
        kind=kind,
        source=intent.source,
        run_id=intent.run_id,
        transition_token=token,
        transition_deadline=deadline,
        reason_detail=reason_detail if isinstance(reason_detail, str) else None,
    )


def decide_node_process(commands: list[Command], facts: DecisionFacts) -> NodeProcessDecision:  # noqa: PLR0911 - the precedence ladder is one return per rung
    by_kind = {c.kind for c in commands}
    if CommandKind.operator_stop in by_kind:
        return NodeProcessDecision("stopped", "hard", _reason(commands, CommandKind.operator_stop))
    if CommandKind.forced_release in by_kind:
        return NodeProcessDecision("stopped", "hard", _reason(commands, CommandKind.forced_release))
    if facts.in_maintenance:
        return NodeProcessDecision("stopping_graceful", "graceful", "maintenance hold")
    if CommandKind.health_failure_stop in by_kind:
        return NodeProcessDecision("stopping_graceful", "graceful", _reason(commands, CommandKind.health_failure_stop))
    starts = [c for c in commands if c.kind in _START_KINDS]
    if facts.device_checks_unhealthy and not starts:
        # The connectivity park: derived from device_checks_healthy IS FALSE and
        # structurally suppressed by any active start command (semantic delta #3).
        return NodeProcessDecision("running_blocked", "defer", "connectivity park")
    if starts:
        # Prefer a token-bearing start (an explicit one-shot restart) over
        # tokenless standing orders; tie-break lexicographically by source —
        # both rules verbatim from the retired arbiter.
        winner = sorted(starts, key=lambda c: (0 if c.transition_token is not None else 1, c.source))[0]
        # A token with no deadline can never be cleared — drop it (verbatim from
        # the retired arbiter). parse_command also drops it; this covers commands
        # built by other means.
        token = winner.transition_token if winner.transition_deadline is not None else None
        return NodeProcessDecision(
            "running",
            None,
            f"{winner.source} command",
            transition_token=token,
            transition_deadline=winner.transition_deadline,
        )
    if facts.in_service:
        return NodeProcessDecision("running", None, "baseline:idle standing start")
    return NodeProcessDecision("stopped", None, "no active node_process command")


def decide_grid_routing(facts: DecisionFacts) -> GridRoutingDecision:
    if facts.reservation_run_id is None:
        return GridRoutingDecision(run_id=None, accepting_new_sessions=True, reason="no reservation routing")
    if facts.cooldown_active:
        return GridRoutingDecision(
            run_id=facts.reservation_run_id, accepting_new_sessions=False, reason="reservation cooldown"
        )
    return GridRoutingDecision(run_id=facts.reservation_run_id, accepting_new_sessions=True, reason="run routing")


def decide_recovery(commands: list[Command], facts: DecisionFacts) -> RecoveryDecision:
    deny = next((c for c in commands if c.kind is CommandKind.operator_recovery_deny), None)
    if deny is not None:
        return RecoveryDecision(False, deny.reason_detail or f"{deny.source} command", deny.source)
    if facts.in_maintenance:
        return RecoveryDecision(False, MAINTENANCE_HOLD_SUPPRESSION_REASON, "maintenance")
    if facts.cooldown_active:
        return RecoveryDecision(False, facts.cooldown_reason, "cooldown")
    allow = next((c for c in commands if c.kind is CommandKind.auto_recovery_allow), None)
    if allow is not None:
        return RecoveryDecision(True, allow.reason_detail or f"{allow.source} command", allow.source)
    return RecoveryDecision(True, None, None)


def map_node_process_decision(decision: NodeProcessDecision) -> tuple[AppiumDesiredState, bool, bool]:
    if decision.desired_state == "running":
        return AppiumDesiredState.running, True, False
    if decision.desired_state == "running_blocked":
        return AppiumDesiredState.running, False, True
    if decision.desired_state == "stopping_graceful":
        return AppiumDesiredState.stopped, False, True
    return AppiumDesiredState.stopped, False, False


def _reason(commands: list[Command], kind: CommandKind) -> str:
    winner = next(c for c in commands if c.kind is kind)
    return f"{winner.source} command"


def _optional_uuid(value: object) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
