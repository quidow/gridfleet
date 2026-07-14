"""Leader-owned reconciler for agent-side Appium processes.

Drives observe-only desired-state convergence per online host: walks
`/agent/health.appium_processes.running_nodes`, counts stray nodes that no
desired row can converge (see ``reconciler_convergence.orphaned_node_ports``),
and ingests agent-reported facts (applied-transition-token, start_failures)
to reconcile each device's desired AppiumNode state. The agent owns
start/stop/restart of its own Appium processes.

Despite the name, unrelated to ``devices.services.intent_reconciler`` (the
``device_intent_reconciler`` loop), which derives desired state from intents
and durable facts — this family only converges toward desired rows it reads.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import agent_nodes_refresh
from app.agent_comm.snapshot import parse_running_nodes
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.appium_nodes.services.reconciler_agent import (
    NodeStartDetails,
    mark_node_started,
    mark_node_stopped,
)
from app.appium_nodes.services.reconciler_allocation import candidate_ports
from app.appium_nodes.services.reconciler_convergence import (
    DesiredRow,
    ObservedEntry,
    _execute_action,
    decide_convergence_action,
    match_observed_entry,
    orphaned_node_ports,
    rows_needing_stale_clear,
    translate_action_for_pull,
)
from app.core.database import async_session
from app.core.metrics_recorders import (
    APPIUM_PULL_MODE_ORPHANS_OBSERVED,
    APPIUM_PULL_MODE_SKIPPED_ACTIONS,
    APPIUM_RECONCILER_HOST_CYCLE_SECONDS,
)
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.hosts.liveness import host_online
from app.hosts.models import Host
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import (
    escalate_device_remediation_failure,
    reset_reconciler_start_failure_if_needed,
)

if TYPE_CHECKING:
    import uuid
    from contextlib import AbstractAsyncContextManager
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)

if TYPE_CHECKING:
    SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]
else:
    SessionScope = Callable[[], object]


def _desired_select() -> Select[Any]:
    target_expr = func.coalesce(Device.connection_target, Device.identity_value)
    return select(
        Device.id.label("device_id"),
        Device.host_id,
        Device.lifecycle_policy_state,
        AppiumNode.id.label("node_id"),
        target_expr.label("connection_target"),
        AppiumNode.desired_state,
        AppiumNode.desired_port,
        AppiumNode.port,
        AppiumNode.pid,
        AppiumNode.started_at,
        AppiumNode.active_connection_target,
        AppiumNode.stop_pending,
    ).join(AppiumNode, AppiumNode.device_id == Device.id)


def _row_to_desired(row: Any, *, reconciler_failure_present: bool = False) -> DesiredRow:  # noqa: ANN401
    return DesiredRow(
        device_id=row.device_id,
        host_id=row.host_id,
        node_id=row.node_id,
        connection_target=row.connection_target,
        desired_state=row.desired_state.value,
        desired_port=row.desired_port,
        port=row.port,
        pid=row.pid,
        started_at=row.started_at,
        active_connection_target=row.active_connection_target,
        stop_pending=row.stop_pending,
        lifecycle_policy_state=row.lifecycle_policy_state,
        reconciler_failure_present=reconciler_failure_present,
    )


async def fetch_desired_rows_for_host(db: AsyncSession, host_id: uuid.UUID) -> list[DesiredRow]:
    stmt = _desired_select().where(Device.host_id == host_id)
    rows = (await db.execute(stmt)).all()
    ladders = await remediation_log.load_ladders(db, [row.device_id for row in rows])
    return [
        _row_to_desired(
            row,
            reconciler_failure_present=(
                ladders[row.device_id].last_failure_source == "appium_reconciler"
                and ladders[row.device_id].last_failure_reason is not None
            ),
        )
        for row in rows
    ]


async def converge_pushed_host(
    *,
    session_factory: SessionFactory,
    reconciler: ReconcilerProtocol,
    host_id: uuid.UUID,
    host_ip: str,
    agent_port: int,
    payload: dict[str, Any],
) -> None:
    """Converge one host from the observation that its status push proved it alive."""
    async with session_factory() as db:
        rows = await fetch_desired_rows_for_host(db, host_id)
        backoff = await remediation_log.load_active_backoffs(db, now=now_utc())
    await reconciler.reconcile_host(
        host_id=host_id,
        host_ip=host_ip,
        agent_port=agent_port,
        rows=rows,
        backoff_until_by_device=backoff,
        payload=payload,
    )


async def _fetch_desired_row(db: AsyncSession, device_id: uuid.UUID) -> DesiredRow | None:
    stmt = _desired_select().where(Device.id == device_id)
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    ladder = await remediation_log.load_ladder(db, row.device_id)
    return _row_to_desired(
        row,
        reconciler_failure_present=(
            ladder.last_failure_source == "appium_reconciler" and ladder.last_failure_reason is not None
        ),
    )


def _session_scope(db: AsyncSession | None) -> SessionScope:
    if db is None:
        return async_session

    @asynccontextmanager
    async def _reuse_session() -> AsyncIterator[AsyncSession]:
        yield db

    return _reuse_session


async def _load_device_for_reconciler(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    result = await db.execute(
        select(Device)
        .where(Device.id == device_id)
        .options(selectinload(Device.host), selectinload(Device.appium_node))
    )
    return result.scalar_one_or_none()


async def _lock_device_for_reconciler(db: AsyncSession, device_id: uuid.UUID) -> Device | None:
    # The device row can be deleted between a start attempt and the failure
    # write (e.g. verification cleanup removing a candidate device). Treat
    # that as "nothing to record" — every caller already handles None.
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound:
        logger.info("reconciler_lock_device_missing", extra={"device_id": str(device_id)})
        return None


def _running_rows_by_target(rows: list[DesiredRow]) -> dict[str, DesiredRow]:
    """Index desired-running rows by connection target for start-failure matching.

    Also indexes by ``active_connection_target`` as a fallback, though a
    failed start normally reports no active target.
    """
    running_by_target: dict[str, DesiredRow] = {}
    for row in rows:
        if row.desired_state != "running":
            continue
        running_by_target[row.connection_target] = row
        if row.active_connection_target:
            running_by_target.setdefault(row.active_connection_target, row)
    return running_by_target


async def _repin_desired_port(
    db: AsyncSession, row: DesiredRow, *, conflict_port: int, settings: SettingsReader
) -> None:
    device = await _lock_device_for_reconciler(db, row.device_id)
    if device is None or device.appium_node is None:
        return
    node = device.appium_node
    try:
        ports = await candidate_ports(db, host_id=row.host_id, exclude_ports={conflict_port}, settings=settings)
    except NodeManagerError:
        logger.warning(
            "appium_reconciler_repin_no_free_ports",
            device_id=str(row.device_id),
            host_id=str(row.host_id),
            conflict_port=conflict_port,
        )
        return
    # Preserve the existing watermark: this write only corrects the port under
    # the same restart request; it is not a competing writer.
    await write_desired_state(
        db,
        node=node,
        caller="appium_reconciler",
        write=DesiredStateWrite(
            target=node.desired_state,
            desired_port=ports[0],
            restart_requested_at=node.restart_requested_at,
        ),
    )
    await db.commit()


async def _touch_last_observed(
    rows: list[DesiredRow], *, settings: SettingsReader, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    if not rows:
        return
    # WI-4 ruling: this observation touch is intentionally LOCKLESS (no
    # lock_appium_node), unlike every other observation-column writer. It is a
    # monotonic timestamp written by the single leader-serialized reconciler and
    # read by no decision logic (display/export only), so a lost update is
    # harmless and self-heals next tick; locking N rows per tick would add
    # contention for nothing. TRIPWIRE: if any loop/allocator/reaper ever starts
    # reading last_observed_at to make a decision, revisit this ruling and WI-2
    # (the guard cannot see this Core write either).
    async with session_factory() as db:
        node_ids = [row.node_id for row in rows]
        await db.execute(update(AppiumNode).where(AppiumNode.id.in_(node_ids)).values(last_observed_at=now_utc()))
        await db.commit()


async def _record_start_failure(
    row: DesiredRow,
    *,
    reason: str,
    session_scope: SessionScope | None = None,
    settings: SettingsReader,
) -> None:
    resolved_session_scope = session_scope or async_session
    async with resolved_session_scope() as db:
        device = await _lock_device_for_reconciler(db, row.device_id)
        if device is None:
            return
        await escalate_device_remediation_failure(
            db, device, settings=settings, source="appium_reconciler", reason=reason
        )
        await db.commit()


async def _reset_start_failure(
    row: DesiredRow,
    *,
    session_scope: SessionScope | None = None,
    settings: SettingsReader,
) -> None:
    resolved_session_scope = session_scope or async_session
    async with resolved_session_scope() as db:
        device = await _lock_device_for_reconciler(db, row.device_id)
        if device is None:
            return
        if not await reset_reconciler_start_failure_if_needed(db, device):
            return
        await db.commit()


class ReconcilerService:
    """Injectable service wrapping Appium desired-state convergence."""

    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        pool: AgentHttpPool | None,
        circuit_breaker: CircuitBreakerProtocol,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._pool = pool
        self._circuit_breaker = circuit_breaker
        self._session_factory = session_factory
        # Sweep-local dedupe cursor for agent-reported start_failures (Task 4):
        # keyed by device_id, holds the max ``at`` already processed so the
        # same ring entry lingering across sweeps doesn't re-fire the re-pin
        # or backoff increment. A scheduler restart resets this in-memory
        # map, re-processing at most one stale report — harmless, the
        # backoff window in ``_record_start_failure`` absorbs it.
        self._last_seen_failure_at: dict[uuid.UUID, str] = {}

    async def reconcile_host(
        self,
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        rows: list[DesiredRow],
        backoff_until_by_device: dict[uuid.UUID, datetime],
        payload: dict[str, object],
    ) -> None:
        """Converge desired Appium nodes on one host from an agent health payload.

        Observe-only: no agent start/stop/restart or orphan reaps are issued
        here — the agent owns those transitions and reports the result as
        observed facts (start_failures) that this
        pass ingests. See ``translate_action_for_pull`` and
        ``_ingest_start_failure_reports``.
        """
        now = now_utc()
        cycle_start = time.monotonic()
        try:
            appium_processes = payload.get("appium_processes")
            if not isinstance(appium_processes, dict):
                return
            running = parse_running_nodes(appium_processes)
            observed = [
                ObservedEntry(
                    port=entry.port,
                    pid=entry.pid,
                    connection_target=entry.connection_target,
                    started_at=entry.started_at,
                )
                for entry in running
            ]
            raw_start_failures = appium_processes.get("start_failures")
            if not isinstance(raw_start_failures, list):
                raw_start_failures = []
            await _touch_last_observed(rows, settings=self._settings, session_factory=self._session_factory)
            # Count stray agent nodes (duplicates for one target, or nodes for
            # a device not on this host) before convergence. Keyed off ALL
            # host rows (rows), not the active subset, so a node for a device
            # in recovery backoff is never mistaken for an orphan. The host
            # owns its own orphan cleanup — the backend only counts what it
            # observes, it never stops anything.
            known_targets = {row.connection_target for row in rows} | {
                row.active_connection_target for row in rows if row.active_connection_target
            }
            orphans = orphaned_node_ports(observed, known_targets=known_targets)
            if orphans:
                APPIUM_PULL_MODE_ORPHANS_OBSERVED.inc(len(orphans))
            # Clear leaked observed pids for devices excluded from active
            # convergence (in recovery backoff). The active loop below never
            # reaches them, so a node stopped during backoff keeps a stale
            # pid in the DB — which blocks an operator start ("node already
            # running"). DB-only clear; never starts/stops an agent node.
            backoff_rows = [
                row
                for row in rows
                if (backoff_until := backoff_until_by_device.get(row.device_id)) is not None and backoff_until > now
            ]
            stale_rows = rows_needing_stale_clear(backoff_rows, observed, now=now)
            if stale_rows:
                clear_observed = self._write_observed_factory()
                for row in stale_rows:
                    await clear_observed(row=row, state="stopped", port=None, pid=None, active_connection_target=None)
            active_rows = [
                row
                for row in rows
                if (backoff_until := backoff_until_by_device.get(row.device_id)) is None or backoff_until <= now
            ]
            if not active_rows:
                return
            # Persists the token clear (in the DB) for any node the agent
            # confirms it applied, before convergence. Note this does NOT
            # mutate the in-memory active_rows snapshots, so this pass's
            # decide_convergence_action still sees the old token and returns
            # restart — harmless only because translate_action_for_pull maps
            # restart -> None; the observed-column sync lands on the next
            # cycle's fresh fetch_desired_rows. Scoped to active_rows
            # (backoff-excluded rows never converge this cycle anyway).
            await self._ingest_start_failure_reports(active_rows, raw_start_failures)
            await self.converge_host_rows(
                None,
                active_rows,
                observed,
                host_id=host_id,
                host_ip=host_ip,
                agent_port=agent_port,
            )
        finally:
            APPIUM_RECONCILER_HOST_CYCLE_SECONDS.labels(host_id=str(host_id)).observe(time.monotonic() - cycle_start)

    async def converge_host_rows(
        self,
        db: AsyncSession | None,
        desired_rows: list[DesiredRow],
        observed: list[ObservedEntry],
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        raise_errors: bool = False,
    ) -> None:
        """Drive convergence for one host."""
        session_scope = _session_scope(db)
        write_observed = self._write_observed_factory(session_scope=session_scope)
        reset_start_failure = self._make_reset_start_failure(session_scope=session_scope)
        observed_by_target = {entry.connection_target: entry for entry in observed}
        observed_by_port = {entry.port: entry for entry in observed}
        for row in sorted(desired_rows, key=lambda item: str(item.device_id)):
            obs = match_observed_entry(row, observed_by_target, observed_by_port)
            action = decide_convergence_action(row, observed=obs, now=now_utc())
            translated = translate_action_for_pull(action)
            if translated is None:
                APPIUM_PULL_MODE_SKIPPED_ACTIONS.labels(kind=action.kind).inc()
                continue
            action = translated
            try:
                await _execute_action(
                    host_id=host_id,
                    row=row,
                    action=action,
                    write_observed=write_observed,
                    reset_start_failure=reset_start_failure,
                )
            except Exception:  # convergence loop; log and continue, re-raise if requested
                logger.warning(
                    "appium_reconciler_convergence_action_failed",
                    exc_info=True,
                    host_id=str(host_id),
                    device_id=str(row.device_id),
                    action=action.kind,
                )
                if raise_errors:
                    raise

    async def _ingest_start_failure_reports(
        self,
        rows: list[DesiredRow],
        start_failures: list[dict[str, Any]] | None = None,
    ) -> None:
        """Ingest agent-reported start-failure facts for pull-only orchestration.

        Mark-stopped-on-absence is already handled by the
        ``db_clear_stale_running`` pass-through and is out of scope here.
        """
        await self._ingest_start_failures(rows, start_failures or [])

    def _match_new_start_failure(
        self, failure: dict[str, Any], running_by_target: dict[str, DesiredRow]
    ) -> tuple[DesiredRow, str, object] | None:
        """Resolve one raw ``start_failures`` entry to ``(row, kind, port)`` if it
        matches a desired-running row and is newer than the dedupe cursor for
        that device — updating the cursor as a side effect. Returns ``None``
        for anything unmatched, malformed, or already-seen (level-style dedupe).
        """
        if not isinstance(failure, dict):
            return None
        target = failure.get("connection_target")
        at = failure.get("at")
        kind = failure.get("kind")
        if not isinstance(target, str) or not isinstance(at, str) or not isinstance(kind, str):
            return None
        row = running_by_target.get(target)
        if row is None:
            return None
        if at <= self._last_seen_failure_at.get(row.device_id, ""):
            return None
        self._last_seen_failure_at[row.device_id] = at
        return row, kind, failure.get("port")

    async def _ingest_start_failures(self, rows: list[DesiredRow], start_failures: list[dict[str, Any]]) -> None:
        """Ingest agent-reported ``start_failures`` (D3): a ``port_conflict`` re-pins
        ``desired_port`` to the next free candidate and trips the existing
        start-failure backoff; a ``spawn_failed`` trips backoff only.

        Only rows desired ``running`` can have a start failure. A failed start
        has no ``active_connection_target``, so failures match by
        ``connection_target`` (falling back to ``active_connection_target`` for
        safety). Dedupe is level-style: see ``_match_new_start_failure``.
        """
        if not start_failures:
            return
        running_by_target = _running_rows_by_target(rows)
        if not running_by_target:
            return
        for failure in start_failures:
            matched = self._match_new_start_failure(failure, running_by_target)
            if matched is None:
                continue
            row, kind, port = matched
            if kind == "port_conflict":
                await _record_start_failure(
                    row, reason="port_conflict", session_scope=self._session_factory, settings=self._settings
                )
                if isinstance(port, int):
                    async with self._session_factory() as db:
                        await _repin_desired_port(db, row, conflict_port=port, settings=self._settings)
            elif kind == "spawn_failed":
                await _record_start_failure(
                    row, reason="spawn_failed", session_scope=self._session_factory, settings=self._settings
                )

    def _write_observed_factory(
        self,
        *,
        session_scope: SessionScope | None = None,
    ) -> Callable[..., Awaitable[None]]:
        resolved_session_scope = session_scope or self._session_factory

        async def _write(
            *,
            row: DesiredRow,
            state: str,
            port: int | None,
            pid: int | None,
            active_connection_target: str | None,
            started_at: datetime | None = None,
            clear_desired_port: bool = False,
            allocated_caps: object = None,
        ) -> None:
            async with resolved_session_scope() as db:
                device = await _load_device_for_reconciler(db, row.device_id)
                if device is None:
                    return
                if state == "running":
                    await mark_node_started(
                        db,
                        device,
                        port=port or row.port or 0,
                        pid=pid,
                        details=NodeStartDetails(
                            active_connection_target=active_connection_target,
                            started_at=started_at,
                            allocated_caps=allocated_caps if isinstance(allocated_caps, dict) else None,
                        ),
                        publisher=self._publisher,
                        settings=self._settings,
                    )
                else:
                    await mark_node_stopped(db, device, publisher=self._publisher)
                if clear_desired_port:
                    device = await _lock_device_for_reconciler(db, row.device_id)
                    if device is None or device.appium_node is None:
                        return
                    node = device.appium_node
                    await write_desired_state(
                        db,
                        node=node,
                        caller="appium_reconciler",
                        write=DesiredStateWrite(
                            target=node.desired_state,
                            desired_port=None if clear_desired_port else node.desired_port,
                            restart_requested_at=node.restart_requested_at,
                        ),
                    )
                    await db.commit()

        return _write

    def _make_reset_start_failure(
        self,
        *,
        session_scope: SessionScope | None = None,
    ) -> Callable[..., Awaitable[None]]:
        async def _reset(*, row: DesiredRow) -> None:
            await _reset_start_failure(row, session_scope=session_scope, settings=self._settings)

        return _reset

    async def converge_device_now(self, device_id: uuid.UUID, *, db: AsyncSession | None = None) -> AppiumNode | None:
        """Run one desired-state convergence pass for a single operator-requested device.

        The periodic leader loop remains the durable fallback. This path only removes
        operator-visible latency after a route has already accepted and committed a
        desired-state change.
        """
        session_scope = _session_scope(db)
        async with session_scope() as read_db:
            row = await _fetch_desired_row(read_db, device_id)
            if row is None:
                return None
            host = await read_db.get(Host, row.host_id)
            if host is None or not host_online(
                host, offline_after_sec=self._settings.get_float("general.host_offline_after_sec")
            ):
                return None

        # No agent start/stop/restart I/O here — just wake the agent's own
        # poller so it re-pulls desired state now.
        try:
            await agent_nodes_refresh(
                host.ip,
                host.agent_port,
                pool=self._pool,
                circuit_breaker=self._circuit_breaker,
            )
        except Exception:  # noqa: BLE001 - poke is best-effort
            logger.debug("agent nodes refresh poke failed for host %s", host.id, exc_info=True)
        async with session_scope() as read_db:
            node = await read_db.get(AppiumNode, row.node_id)
            if node is not None:
                await read_db.refresh(node)
            return node
