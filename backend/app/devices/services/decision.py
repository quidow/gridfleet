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

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.appium_nodes.models import AppiumDesiredState
from app.core.observability import get_logger
from app.devices.services.intent_types import VERIFICATION_OUTCOME_KEY, CommandKind
from app.devices.services.lifecycle_policy_state import MAINTENANCE_HOLD_SUPPRESSION_REASON
from app.lifecycle.services.remediation_log import DIRECTIVE_START, DIRECTIVE_STOP

if TYPE_CHECKING:
    import uuid
    from typing import Any, Protocol

    from app.lifecycle.services.remediation_log import NodeDirective

    class IntentLike(Protocol):
        device_id: uuid.UUID
        source: str
        kind: str
        run_id: uuid.UUID | None
        payload: dict[str, Any]
        expires_at: datetime | None


logger = get_logger(__name__)

NodeProcessState = str  # "running" | "running_draining" | "running_blocked" | "stopping_graceful" | "stopped"
StopMode = str  # "hard" | "graceful" | "defer"


_START_KINDS = frozenset({CommandKind.operator_start, CommandKind.verification_start})


@dataclass(frozen=True)
class Command:
    kind: CommandKind
    source: str
    run_id: uuid.UUID | None
    restart_requested_at: datetime | None
    reason_detail: str | None


@dataclass(frozen=True)
class DecisionFacts:
    in_maintenance: bool
    device_checks_unhealthy: bool  # device_checks_healthy IS FALSE
    in_service: bool  # WithdrawalFacts.in_service(): baseline eligibility (F-G1)
    reservation_run_id: uuid.UUID | None  # None when unreserved OR indefinitely excluded
    cooldown_active: bool  # excluded AND excluded_until > now
    cooldown_reason: str | None
    remediation_directive: NodeDirective | None = None  # derived from device_remediation_log (WS-15.2)


@dataclass(frozen=True)
class NodeProcessDecision:
    desired_state: NodeProcessState
    stop_mode: StopMode | None
    reason: str
    restart_requested_at: datetime | None = None


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


def parse_command(intent: IntentLike, now: datetime) -> Command | None:
    """Parse a stored row into a typed command; None for expired or unknown rows.

    Unknown kinds are logged and ignored (the retired arbiter ranked them at
    priority 0, where they could never win either).
    """
    if intent.expires_at is not None and intent.expires_at <= now:
        return None
    if intent.payload.get(VERIFICATION_OUTCOME_KEY) is not None:
        # A finalized lease is a tombstone awaiting deletion, not a command:
        # verification stamps its terminal outcome at finalization (WS-15.3).
        return None
    try:
        kind = CommandKind(intent.kind)
    except ValueError:
        logger.warning("device_intent_unknown_kind", kind=intent.kind, device_id=str(intent.device_id))
        return None
    restart_requested_at = _optional_datetime(intent.payload.get("restart_requested_at"))
    reason_detail = intent.payload.get("reason")
    return Command(
        kind=kind,
        source=intent.source,
        run_id=intent.run_id,
        restart_requested_at=restart_requested_at,
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
    starts = [c for c in commands if c.kind in _START_KINDS]
    directive = facts.remediation_directive
    # A derived start is in_service-gated: a shelved or withdrawn device must never
    # keep a directive-started node alive past the episode.
    remediation_start = directive is not None and directive.kind == DIRECTIVE_START and facts.in_service
    if directive is not None and directive.kind == DIRECTIVE_STOP and not starts and not remediation_start:
        # Derived failure-stop: an auto-stop episode holds the node stopped until a
        # reset supersedes it. Any active start — stored or derived — outranks it
        # structurally, replacing the retired revoke-before-start rituals.
        return NodeProcessDecision("stopping_graceful", "graceful", directive.reason or "remediation auto-stop")
    if facts.device_checks_unhealthy and not starts and not remediation_start:
        # The connectivity park: derived from device_checks_healthy IS FALSE and
        # structurally suppressed by any active start command (semantic delta #3).
        return NodeProcessDecision("running_blocked", "defer", "connectivity park")
    if starts or remediation_start:
        # Newest watermark wins: a later restart request supersedes an earlier
        # one (restart-at-least-once-after-T is monotone), so last-write-wins
        # is correct and the old token override-detection has nothing to detect.
        watermarks = [c.restart_requested_at for c in starts if c.restart_requested_at is not None]
        if remediation_start and directive is not None and directive.restart_watermark is not None:
            watermarks.append(directive.restart_watermark)
        watermark = max(watermarks, default=None)
        if starts:
            winner = max(
                starts,
                key=lambda c: (
                    c.restart_requested_at is not None,
                    c.restart_requested_at or datetime.min.replace(tzinfo=UTC),
                    c.source,
                ),
            )
            reason = f"{winner.source} command"
        else:
            reason = "remediation recovery start"
        return NodeProcessDecision("running", None, reason, restart_requested_at=watermark)
    if facts.in_service:
        rollout = next((c for c in commands if c.kind is CommandKind.release_rollout), None)
        if rollout is not None:
            return NodeProcessDecision(
                "running_draining",
                None,
                f"{rollout.source} release rollout",
                restart_requested_at=rollout.restart_requested_at,
            )
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
    return RecoveryDecision(True, None, None)


def map_node_process_decision(decision: NodeProcessDecision) -> tuple[AppiumDesiredState, bool, bool]:
    if decision.desired_state == "running":
        return AppiumDesiredState.running, True, False
    if decision.desired_state == "running_draining":
        return AppiumDesiredState.running, False, False
    if decision.desired_state == "running_blocked":
        return AppiumDesiredState.running, False, True
    if decision.desired_state == "stopping_graceful":
        return AppiumDesiredState.stopped, False, True
    return AppiumDesiredState.stopped, False, False


def _reason(commands: list[Command], kind: CommandKind) -> str:
    winner = next(c for c in commands if c.kind is kind)
    return f"{winner.source} command"


def _optional_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
