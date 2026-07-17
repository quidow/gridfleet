"""Desired-state convergence decisions for the Appium reconciler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from app.core.metrics_recorders import APPIUM_RECONCILER_CONVERGENCE_ACTIONS
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
    port: int | None
    pid: int | None
    active_connection_target: str | None
    stop_pending: bool
    started_at: datetime | None = None
    observed_pack_release: str | None = None
    # Carried lock-free from the desired-rows SELECT for the confirm_running pre-check.
    lifecycle_policy_state: dict[str, Any] | None = field(default=None)
    reconciler_failure_present: bool = False


@dataclass(frozen=True, slots=True)
class ObservedEntry:
    port: int
    pid: int | None
    connection_target: str
    started_at: datetime | None = None
    pack_release: str | None = None


ActionKind = Literal[
    "start",
    "stop",
    "no_op",
    "confirm_running",
    "db_mark_running",
    "db_clear_stale_running",
]


@dataclass(frozen=True, slots=True)
class ConvergenceAction:
    kind: ActionKind
    port: int | None = None
    start_port: int | None = None
    pid: int | None = None
    started_at: datetime | None = None
    pack_release: str | None = None
    active_connection_target: str | None = None
    clear_desired_port: bool = False


WriteObserved = Callable[..., Awaitable[None]]
ResetStartFailure = Callable[..., Awaitable[None]]


def _decide_running_action(
    row: DesiredRow,
    *,
    observed: ObservedEntry | None,
) -> ConvergenceAction:
    """Convergence action when the desired state is ``running``."""
    if observed is None:
        return ConvergenceAction(kind="start", port=row.desired_port)
    if row.desired_port is None or observed.port == row.desired_port:
        if (
            row.port != observed.port
            or row.pid != observed.pid
            or (observed.started_at is not None and row.started_at != observed.started_at)
            or (observed.pack_release is not None and row.observed_pack_release != observed.pack_release)
            or row.active_connection_target != observed.connection_target
        ):
            return ConvergenceAction(
                kind="db_mark_running",
                port=observed.port,
                pid=observed.pid,
                started_at=observed.started_at,
                pack_release=observed.pack_release,
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
    if row.desired_state == "running":
        return _decide_running_action(row, observed=observed)

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


def match_observed_entry(
    row: DesiredRow,
    observed_by_target: dict[str, ObservedEntry],
    observed_by_port: dict[int, ObservedEntry] | None = None,
) -> ObservedEntry | None:
    """Return the agent-reported node for ``row``, if any.

    A running node may report either the device's registered connection_target
    (real devices) or a live target resolved at start time (virtual emulators
    report their ADB serial, not the AVD name). Prefer the row's recorded live
    target, then fall back to the registered one, then to the node's port.

    The port fallback bridges the emulator case when ``active_connection_target``
    is unset (e.g. cleared by the recovery-backoff stale-clear): the observation
    is keyed by the ADB serial while the row holds only the AVD name, so neither
    target lookup hits. The node's port is its stable identity — matching on it
    lets the observed pid/target fold instead of stranding the node with
    ``observed_running`` False forever (the recovery deadlock).
    """
    if row.active_connection_target is not None:
        entry = observed_by_target.get(row.active_connection_target)
        if entry is not None:
            return entry
    entry = observed_by_target.get(row.connection_target)
    if entry is not None:
        return entry
    if observed_by_port is not None and row.port is not None:
        return observed_by_port.get(row.port)
    return None


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
    observed_by_port = {entry.port: entry for entry in observed}
    return [
        row
        for row in rows
        if decide_convergence_action(
            row, observed=match_observed_entry(row, observed_by_target, observed_by_port), now=now
        ).kind
        == "db_clear_stale_running"
    ]


def translate_action_for_pull(action: ConvergenceAction) -> ConvergenceAction | None:
    """Translate a convergence action for pull-only orchestration.

    Agent-touching kinds (``start``/``stop``) are skipped: the
    agent owns those transitions and reports the result as observed facts on
    its next health payload. DB-only kinds pass through unchanged.
    """
    if action.kind in ("start", "stop"):
        return None
    return action


async def _execute_action(
    *,
    host_id: uuid.UUID,
    row: DesiredRow,
    action: ConvergenceAction,
    write_observed: WriteObserved,
    reset_start_failure: ResetStartFailure,
) -> None:
    """Dispatch one DB-only convergence action.

    ``translate_action_for_pull`` is the only action path into this function:
    it strips agent-touching kinds (``start``/``stop``) before a
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
        if row.reconciler_failure_present:
            await reset_start_failure(row=row)
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
            started_at=action.started_at,
            pack_release=action.pack_release,
            active_connection_target=action.active_connection_target,
        )
        return
