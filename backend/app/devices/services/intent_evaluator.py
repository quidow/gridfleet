from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Literal, TypeGuard

from app.appium_nodes.models import AppiumDesiredState
from app.core import metrics_recorders
from app.core.observability import get_logger

if TYPE_CHECKING:
    from app.devices.models import DeviceIntent

logger = get_logger(__name__)

NodeProcessState = Literal["running", "running_blocked", "stopping_graceful", "stopped"]
StopMode = Literal["hard", "graceful", "defer"]


@dataclass(frozen=True)
class NodeProcessDecision:
    desired_state: NodeProcessState
    desired_port: int | None
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
class ReservationDecision:
    excluded: bool
    run_id: uuid.UUID | None
    exclusion_reason: str | None
    cooldown_count: int | None
    expires_at: datetime | None
    reason: str


@dataclass(frozen=True)
class RecoveryDecision:
    allowed: bool
    reason: str | None
    source: str | None


def evaluate_node_process(intents: list[DeviceIntent], now: datetime) -> NodeProcessDecision:
    active = _active_intents(intents, now)
    if not active:
        return NodeProcessDecision(
            desired_state="stopped",
            desired_port=None,
            stop_mode=None,
            reason="no active node_process intent",
        )

    highest_priority = max(_priority(intent) for intent in active)
    winners = [intent for intent in active if _priority(intent) == highest_priority]
    actions = {_action(intent) for intent in winners}
    if actions == {"start", "stop"}:
        sources = sorted(intent.source for intent in winners)
        logger.warning(
            "device_intent_same_priority_conflict",
            priority=highest_priority,
            sources=sources,
        )
        metrics_recorders.INTENT_RECONCILER_CONFLICTS.inc()
        return NodeProcessDecision(
            desired_state="stopped",
            desired_port=None,
            stop_mode="hard",
            reason=f"same priority node_process conflict at priority {highest_priority}",
        )

    winner = sorted(winners, key=lambda intent: intent.source)[0]
    if _action(winner) == "start":
        transition_token = _optional_uuid(winner.payload.get("transition_token"))
        transition_deadline = _optional_datetime(winner.payload.get("transition_deadline"))
        if transition_token is not None and transition_deadline is None:
            transition_token = None
        return NodeProcessDecision(
            desired_state="running",
            desired_port=_optional_int(winner.payload.get("desired_port")),
            stop_mode=None,
            reason=_intent_reason(winner),
            transition_token=transition_token,
            transition_deadline=transition_deadline,
        )

    stop_mode = _stop_mode(winner.payload.get("stop_mode"))
    if stop_mode == "defer":
        desired_state: NodeProcessState = "running_blocked"
    elif stop_mode == "graceful":
        desired_state = "stopping_graceful"
    else:
        desired_state = "stopped"
    return NodeProcessDecision(
        desired_state=desired_state,
        desired_port=None,
        stop_mode=stop_mode,
        reason=_intent_reason(winner),
    )


def map_node_process_decision(decision: NodeProcessDecision) -> tuple[AppiumDesiredState, bool, bool]:
    if decision.desired_state == "running":
        return AppiumDesiredState.running, True, False
    if decision.desired_state == "running_blocked":
        return AppiumDesiredState.running, False, True
    if decision.desired_state == "stopping_graceful":
        return AppiumDesiredState.stopped, False, True
    return AppiumDesiredState.stopped, False, False


def evaluate_grid_routing(intents: list[DeviceIntent], now: datetime) -> GridRoutingDecision:
    winner = _highest_active(intents, now)
    if winner is None:
        return GridRoutingDecision(run_id=None, accepting_new_sessions=True, reason="no active grid_routing intent")
    return GridRoutingDecision(
        run_id=winner.run_id,
        accepting_new_sessions=bool(winner.payload.get("accepting_new_sessions", True)),
        reason=_intent_reason(winner),
    )


def evaluate_reservation(intents: list[DeviceIntent], now: datetime) -> ReservationDecision:
    winner = _highest_active([intent for intent in intents if bool(intent.payload.get("excluded", True))], now)
    if winner is None:
        return ReservationDecision(
            excluded=False,
            run_id=None,
            exclusion_reason=None,
            cooldown_count=None,
            expires_at=None,
            reason="no active reservation exclusion intent",
        )
    return ReservationDecision(
        excluded=True,
        run_id=winner.run_id,
        exclusion_reason=_optional_str(winner.payload.get("exclusion_reason")),
        cooldown_count=_optional_int(winner.payload.get("cooldown_count")),
        expires_at=winner.expires_at,
        reason=_intent_reason(winner),
    )


def evaluate_recovery(intents: list[DeviceIntent], now: datetime) -> RecoveryDecision:
    active = _active_intents(intents, now)
    if not active:
        return RecoveryDecision(allowed=True, reason=None, source=None)
    highest_priority = max(_priority(intent) for intent in active)
    winners = [intent for intent in active if _priority(intent) == highest_priority]
    # Deny wins only within the highest priority tier, so low-priority safety
    # blocks do not override explicit higher-priority operator recovery intent.
    deny_winner = _highest([intent for intent in winners if not bool(intent.payload.get("allowed", True))])
    if deny_winner is not None:
        return RecoveryDecision(
            allowed=False,
            reason=_optional_str(deny_winner.payload.get("reason")) or _intent_reason(deny_winner),
            source=deny_winner.source,
        )
    allow_winner = _highest(winners)
    if allow_winner is None:  # pragma: no cover - winners is non-empty, kept for type narrowing.
        return RecoveryDecision(allowed=True, reason=None, source=None)
    return RecoveryDecision(
        allowed=True,
        reason=_optional_str(allow_winner.payload.get("reason")) or _intent_reason(allow_winner),
        source=allow_winner.source,
    )


def _active_intents(intents: list[DeviceIntent], now: datetime) -> list[DeviceIntent]:
    return [intent for intent in intents if intent.expires_at is None or intent.expires_at > now]


def _highest_active(intents: list[DeviceIntent], now: datetime) -> DeviceIntent | None:
    return _highest(_active_intents(intents, now))


def _highest(intents: list[DeviceIntent]) -> DeviceIntent | None:
    if not intents:
        return None
    return sorted(intents, key=lambda intent: (-_priority(intent), intent.source))[0]


def _priority(intent: DeviceIntent) -> int:
    return _optional_int(intent.payload.get("priority")) or 0


def _action(intent: DeviceIntent) -> Literal["start", "stop"]:
    action = intent.payload.get("action")
    if action == "start":
        return "start"
    return "stop"


def _stop_mode(value: object) -> StopMode:
    if _is_stop_mode(value):
        return value
    return "hard"


def _is_stop_mode(value: object) -> TypeGuard[StopMode]:
    return value in {"hard", "graceful", "defer"}


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_str(value: object) -> str | None:
    if isinstance(value, str):
        return value
    return None


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
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed


def _intent_reason(intent: DeviceIntent) -> str:
    return f"{intent.source} intent (priority {_priority(intent)})"
