"""Desired-state convergence decisions for the Appium reconciler."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from app.metrics_recorders import (
    APPIUM_RECONCILER_CONVERGENCE_ACTIONS,
    APPIUM_RECONCILER_TRANSITION_TOKEN_EXPIRED,
)
from app.observability import get_logger

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
            return ConvergenceAction(kind="no_op")
        return ConvergenceAction(kind="stop", port=observed.port, clear_desired_port=True)

    if observed is not None:
        return ConvergenceAction(kind="stop", port=observed.port)
    if row.pid is not None or row.active_connection_target is not None:
        return ConvergenceAction(kind="db_clear_stale_running")
    return ConvergenceAction(kind="no_op")


async def converge_host_rows(
    *,
    host_id: uuid.UUID,
    rows: list[DesiredRow],
    agent_running: list[ObservedEntry],
    now: datetime,
    start_agent: StartAgent,
    stop_agent: StopAgent,
    write_observed: WriteObserved,
    clear_token: ClearToken,
) -> None:
    """Drive convergence for one host.

    Hosts are parallelized by the caller; rows on a single host are processed
    serially so one row's observed-state write is visible to the next row's
    allocator call.
    """
    observed_by_target = {entry.connection_target: entry for entry in agent_running}
    for row in sorted(rows, key=lambda item: str(item.device_id)):
        observed = observed_by_target.get(row.connection_target)
        action = decide_convergence_action(row, observed=observed, now=now)
        try:
            await _execute_action(
                host_id=host_id,
                row=row,
                action=action,
                start_agent=start_agent,
                stop_agent=stop_agent,
                write_observed=write_observed,
                clear_token=clear_token,
            )
        except Exception:
            logger.warning(
                "appium_reconciler_convergence_action_failed",
                exc_info=True,
                host_id=str(host_id),
                device_id=str(row.device_id),
                action=action.kind,
            )


async def _execute_action(
    *,
    host_id: uuid.UUID,
    row: DesiredRow,
    action: ConvergenceAction,
    start_agent: StartAgent,
    stop_agent: StopAgent,
    write_observed: WriteObserved,
    clear_token: ClearToken,
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
        result = await start_agent(row=row, port=action.port)
        await write_observed(
            row=row,
            state="running",
            port=_int_or_none(result.get("port")) or action.port,
            pid=_int_or_none(result.get("pid")),
            active_connection_target=_str_or_none(result.get("active_connection_target")) or row.connection_target,
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
        result = await start_agent(row=row, port=action.start_port)
        await write_observed(
            row=row,
            state="running",
            port=_int_or_none(result.get("port")) or action.start_port,
            pid=_int_or_none(result.get("pid")),
            active_connection_target=_str_or_none(result.get("active_connection_target")) or row.connection_target,
            allocated_caps=result.get("allocated_caps"),
            clear_transition=True,
        )


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _positive_or_none(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None
