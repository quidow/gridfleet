"""Desired-state convergence decisions for the Appium reconciler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.core.metrics_recorders import APPIUM_RECONCILER_CONVERGENCE_ACTIONS, APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED
from app.core.observability import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import uuid
    from datetime import datetime


@dataclass(frozen=True, slots=True)
class DesiredRow:
    device_id: uuid.UUID
    host_id: uuid.UUID
    node_id: uuid.UUID
    connection_target: str
    desired_state: str
    desired_port: int | None
    transition_token: uuid.UUID | None
    transition_deadline: datetime | None
    port: int | None
    pid: int | None
    active_connection_target: str | None
    stop_pending: bool
    # Carried lock-free from the desired-rows SELECT for the confirm_running pre-check.
    lifecycle_policy_state: dict[str, Any] | None = field(default=None)


@dataclass(frozen=True, slots=True)
class ObservedEntry:
    port: int
    pid: int | None
    connection_target: str


ActionKind = Literal[
    "start",
    "stop",
    "restart",
    "no_op",
    "confirm_running",
    "db_mark_running",
    "db_clear_stale_running",
    "clear_expired_token",
]


@dataclass(frozen=True, slots=True)
class ConvergenceAction:
    kind: ActionKind
    port: int | None = None
    stop_port: int | None = None
    start_port: int | None = None
    pid: int | None = None
    active_connection_target: str | None = None
    clear_desired_port: bool = False


def _needs_start_failure_reset(state: dict[str, Any] | None) -> bool:
    """True when ``lifecycle_policy_state`` carries appium-reconciler failure/backoff residue."""
    if state is None:
        return False
    has_reconciler_failure = state.get("last_failure_source") == "appium_reconciler"
    has_orphaned_reason = bool(state.get("last_failure_reason") and not state.get("last_failure_source"))
    return bool(
        state.get("recovery_backoff_attempts")
        or state.get("backoff_until")
        or has_reconciler_failure
        or has_orphaned_reason
    )


WriteObserved = Callable[..., Awaitable[None]]
ClearToken = Callable[..., Awaitable[None]]
ResetStartFailure = Callable[..., Awaitable[None]]


def _decide_running_action(
    row: DesiredRow,
    *,
    observed: ObservedEntry | None,
    token_active: bool,
    token_expired: bool,
) -> ConvergenceAction:
    """Convergence action when the desired state is ``running``."""
    if token_expired:
        return ConvergenceAction(kind="clear_expired_token")
    if token_active:
        return ConvergenceAction(
            kind="restart",
            stop_port=observed.port if observed is not None else None,
            start_port=_positive_or_none(row.desired_port) or _positive_or_none(row.port),
        )
    if observed is None:
        return ConvergenceAction(kind="start", port=row.desired_port)
    if row.desired_port is None or observed.port == row.desired_port:
        if (
            row.port != observed.port
            or row.pid != observed.pid
            or row.active_connection_target != observed.connection_target
        ):
            return ConvergenceAction(
                kind="db_mark_running",
                port=observed.port,
                pid=observed.pid,
                active_connection_target=observed.connection_target,
            )
        return ConvergenceAction(kind="confirm_running")
    return ConvergenceAction(kind="stop", port=observed.port, clear_desired_port=True)


def decide_convergence_action(
    row: DesiredRow,
    *,
    observed: ObservedEntry | None,
    now: datetime,
) -> ConvergenceAction:
    """Return the next action for one desired DB row and one agent observation."""
    token_active = (
        row.transition_token is not None and row.transition_deadline is not None and row.transition_deadline > now
    )
    token_expired = (
        row.transition_token is not None and row.transition_deadline is not None and row.transition_deadline <= now
    )

    if row.desired_state == "running":
        return _decide_running_action(row, observed=observed, token_active=token_active, token_expired=token_expired)

    if observed is not None:
        if row.stop_pending:
            return ConvergenceAction(kind="no_op")
        return ConvergenceAction(kind="stop", port=observed.port)
    if row.pid is not None or row.active_connection_target is not None:
        return ConvergenceAction(kind="db_clear_stale_running")
    return ConvergenceAction(kind="no_op")


def orphaned_node_ports(observed: list[ObservedEntry], *, known_targets: set[str]) -> list[int]:
    """Agent-reported node ports that no desired row can converge.

    The per-row convergence loop matches at most one observed entry per
    ``connection_target`` (last-wins in ``observed_by_target``), so a second node
    for the same target is never reached; and a node whose target has no device
    on the host is never iterated at all. Both linger untracked — the backend then
    health-checks the device's tracked port while the stray node holds another,
    flapping the device offline. These ports must be stopped.

    ``known_targets`` MUST be every device target on the host, including devices
    in recovery backoff (which are excluded from the *active* convergence set) —
    otherwise a legitimate single node for a backoff device would be reaped.
    It must also include each row's ``active_connection_target``: a running node
    may report a live target resolved at start time (virtual emulators report
    their ADB serial, not the AVD name) instead of the registered target.
    """
    primary_port_by_target: dict[str, int] = {entry.connection_target: entry.port for entry in observed}
    orphans: list[int] = []
    for entry in observed:
        is_unknown_target = entry.connection_target not in known_targets
        is_duplicate = primary_port_by_target[entry.connection_target] != entry.port
        if is_unknown_target or is_duplicate:
            orphans.append(entry.port)
    return orphans


def match_observed_entry(row: DesiredRow, observed_by_target: dict[str, ObservedEntry]) -> ObservedEntry | None:
    """Return the agent-reported node for ``row``, if any.

    A running node may report either the device's registered connection_target
    (real devices) or a live target resolved at start time (virtual emulators
    report their ADB serial, not the AVD name). Prefer the row's recorded live
    target, then fall back to the registered one.
    """
    if row.active_connection_target is not None:
        entry = observed_by_target.get(row.active_connection_target)
        if entry is not None:
            return entry
    return observed_by_target.get(row.connection_target)


def rows_needing_stale_clear(
    rows: list[DesiredRow], observed: list[ObservedEntry], *, now: datetime
) -> list[DesiredRow]:
    """Rows whose only safe convergence action is ``db_clear_stale_running``.

    Used to clear leaked observed pids for devices excluded from active
    convergence (in recovery backoff): the agent reports no node for the target
    but the DB still records one. Restricted to the DB-only clear action — never
    an agent start/stop — so a backed-off device's node lifecycle stays with
    recovery.
    """
    observed_by_target = {entry.connection_target: entry for entry in observed}
    return [
        row
        for row in rows
        if decide_convergence_action(row, observed=match_observed_entry(row, observed_by_target), now=now).kind
        == "db_clear_stale_running"
    ]


def translate_action_for_pull(action: ConvergenceAction) -> ConvergenceAction | None:
    """Translate a convergence action for a pull-capable host.

    Agent-touching kinds (``start``/``stop``/``restart``) are skipped: the
    agent owns those transitions and reports the result as observed facts on
    its next health payload. DB-only kinds pass through unchanged.
    """
    if action.kind in ("start", "stop", "restart"):
        return None
    return action


async def _execute_action(
    *,
    host_id: uuid.UUID,
    row: DesiredRow,
    action: ConvergenceAction,
    write_observed: WriteObserved,
    clear_token: ClearToken,
    reset_start_failure: ResetStartFailure,
) -> None:
    """Dispatch one DB-only convergence action.

    ``translate_action_for_pull`` is the only action path into this function:
    it strips agent-touching kinds (``start``/``stop``/``restart``) before a
    row ever reaches here, so every kind handled below is DB-only.
    """
    APPIUM_RECONCILER_CONVERGENCE_ACTIONS.labels(action=action.kind).inc()
    logger.info(
        "appium_reconciler_convergence_action",
        host_id=str(host_id),
        device_id=str(row.device_id),
        action=action.kind,
    )

    if action.kind == "no_op":
        return
    if action.kind == "confirm_running":
        # ponytail: cheap pre-check skips the Device row lock in the steady-state path
        if _needs_start_failure_reset(row.lifecycle_policy_state):
            await reset_start_failure(row=row)
        return
    if action.kind == "clear_expired_token":
        APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED.inc()
        await clear_token(row=row)
        return
    if action.kind == "db_clear_stale_running":
        await write_observed(row=row, state="stopped", port=None, pid=None, active_connection_target=None)
        return
    if action.kind == "db_mark_running":
        await write_observed(
            row=row,
            state="running",
            port=action.port,
            pid=action.pid,
            active_connection_target=action.active_connection_target,
        )
        return


def _positive_or_none(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None
