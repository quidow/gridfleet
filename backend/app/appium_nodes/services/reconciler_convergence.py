"""Desired-state convergence decisions for the Appium reconciler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from app.appium_nodes.exceptions import NodeAlreadyRunningError
from app.core.metrics_recorders import APPIUM_RECONCILER_CONVERGENCE_ACTIONS, APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED
from app.core.observability import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    import uuid
    from datetime import datetime


def _log_already_running(*, host_id: uuid.UUID, row: DesiredRow, action: str) -> None:
    """The agent already runs a node for this target — the start/restart leg is
    a no-op. The next observation tick records the node via ``db_mark_running``."""
    logger.debug(
        "appium_reconciler_node_already_running",
        host_id=str(host_id),
        device_id=str(row.device_id),
        connection_target=row.connection_target,
        action=action,
    )


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


StartAgent = Callable[..., Awaitable[dict[str, Any]]]
StopAgent = Callable[..., Awaitable[None]]
WriteObserved = Callable[..., Awaitable[None]]
ClearToken = Callable[..., Awaitable[None]]
ResetStartFailure = Callable[..., Awaitable[None]]


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
    """
    primary_port_by_target: dict[str, int] = {entry.connection_target: entry.port for entry in observed}
    orphans: list[int] = []
    for entry in observed:
        is_unknown_target = entry.connection_target not in known_targets
        is_duplicate = primary_port_by_target[entry.connection_target] != entry.port
        if is_unknown_target or is_duplicate:
            orphans.append(entry.port)
    return orphans


async def reap_orphan_nodes(
    observed: list[ObservedEntry],
    desired_rows: list[DesiredRow],
    *,
    stop_agent: Callable[..., Awaitable[None]],
) -> list[int]:
    """Stop agent-reported nodes that no desired row can converge.

    See ``orphaned_node_ports`` for what counts as an orphan. A per-port stop
    failure is logged and swallowed so one stray node cannot abort the host's
    reconcile cycle. Returns the ports identified as orphans (for observability
    and tests).
    """
    known_targets = {row.connection_target for row in desired_rows}
    orphans = orphaned_node_ports(observed, known_targets=known_targets)
    for port in orphans:
        logger.warning("appium_reconciler_orphan_node_stop", port=port)
        try:
            await stop_agent(row=None, port=port)
        except Exception:  # noqa: BLE001 — one stray node must not abort the host cycle
            logger.warning("appium_reconciler_orphan_node_stop_failed", exc_info=True, port=port)
    return orphans


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
        if decide_convergence_action(row, observed=observed_by_target.get(row.connection_target), now=now).kind
        == "db_clear_stale_running"
    ]


async def _execute_action(
    *,
    host_id: uuid.UUID,
    row: DesiredRow,
    action: ConvergenceAction,
    start_agent: StartAgent,
    stop_agent: StopAgent,
    write_observed: WriteObserved,
    clear_token: ClearToken,
    reset_start_failure: ResetStartFailure,
) -> None:
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
        await reset_start_failure(row=row)
        return
    if action.kind == "clear_expired_token":
        APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED.inc()
        await clear_token(row=row, reason="deadline_elapsed")
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
    if action.kind == "start":
        try:
            result = await start_agent(row=row, port=action.port)
        except NodeAlreadyRunningError:
            _log_already_running(host_id=host_id, row=row, action=action.kind)
            return
        result_port = _int_or_none(result.get("port"))
        await write_observed(
            row=row,
            state="running",
            port=result_port or action.port,
            pid=_int_or_none(result.get("pid")),
            active_connection_target=_str_or_none(result.get("active_connection_target")) or row.connection_target,
            clear_desired_port=_uses_fallback_port(requested=action.port, actual=result_port),
            allocated_caps=result.get("allocated_caps"),
        )
        return
    if action.kind == "stop":
        await stop_agent(row=row, port=action.port)
        await write_observed(
            row=row,
            state="stopped",
            port=None,
            pid=None,
            active_connection_target=None,
            clear_desired_port=action.clear_desired_port,
            clear_transition=row.transition_token is not None,
        )
        return
    if action.kind == "restart":
        if action.stop_port is not None:
            await stop_agent(row=row, port=action.stop_port)
        try:
            result = await start_agent(row=row, port=action.start_port)
        except NodeAlreadyRunningError:
            _log_already_running(host_id=host_id, row=row, action=action.kind)
            return
        result_port = _int_or_none(result.get("port"))
        await write_observed(
            row=row,
            state="running",
            port=result_port or action.start_port,
            pid=_int_or_none(result.get("pid")),
            active_connection_target=_str_or_none(result.get("active_connection_target")) or row.connection_target,
            allocated_caps=result.get("allocated_caps"),
            clear_desired_port=_uses_fallback_port(requested=action.start_port, actual=result_port),
            clear_transition=True,
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _positive_or_none(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _uses_fallback_port(*, requested: int | None, actual: int | None) -> bool:
    return requested is not None and actual is not None and actual != requested
