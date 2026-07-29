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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.exc import NoResultFound

from app.agent_comm.node_poke import NodeRefreshTarget, poke_node_refresh_target
from app.agent_comm.snapshot import parse_running_nodes
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.appium_nodes.services.locking import lock_appium_node_for_device
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
from app.core.metrics_recorders import (
    APPIUM_PULL_MODE_ORPHANS_OBSERVED,
    APPIUM_PULL_MODE_SKIPPED_ACTIONS,
    APPIUM_RECONCILER_HOST_CYCLE_SECONDS,
)
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services.decision_snapshot import load_device_decision_snapshot
from app.devices.services.readiness import preloaded_pack_catalog
from app.hosts.liveness import host_online
from app.hosts.models import Host
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import (
    escalate_device_remediation_failure,
    reset_reconciler_start_failure_if_needed,
)
from app.packs.services.catalog_view import load_pack_catalog

if TYPE_CHECKING:
    import uuid
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.devices.locking import LockedDevice
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)


def _desired_select() -> Select[Any]:
    target_expr = func.coalesce(Device.connection_target, Device.identity_value)
    return select(
        Device.id.label("device_id"),
        Device.host_id,
        Device.lifecycle_policy_state,
        Device.pack_id,
        AppiumNode.id.label("node_id"),
        target_expr.label("connection_target"),
        AppiumNode.desired_state,
        AppiumNode.desired_port,
        AppiumNode.port,
        AppiumNode.pid,
        AppiumNode.started_at,
        AppiumNode.observed_pack_release,
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
        observed_pack_release=row.observed_pack_release,
        active_connection_target=row.active_connection_target,
        stop_pending=row.stop_pending,
        lifecycle_policy_state=row.lifecycle_policy_state,
        reconciler_failure_present=reconciler_failure_present,
        pack_id=row.pack_id,
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
        # ONE catalog read for the whole host. Every per-device settlement below
        # opens its own session, so a per-device resolve costs a statement each;
        # the projection is value-shaped, so it is legal to carry across those
        # session boundaries (an ORM catalog would not be). A host with no rows
        # buys no statement at all.
        catalog = await load_pack_catalog(db, {row.pack_id for row in rows if row.pack_id})
    # The trade this accepts: a device whose pack row changes mid-cycle is judged
    # against the catalog as of the read above, self-healing next cycle. A device
    # whose own pack_id changed is not affected — a catalog miss re-reads that
    # pack (app.devices.services.readiness.assess_device_async).
    with preloaded_pack_catalog(catalog):
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


async def _lock_device_for_reconciler(db: AsyncSession, device_id: uuid.UUID) -> LockedDevice | None:
    # The device row can be deleted between a start attempt and the failure
    # write (e.g. verification cleanup removing a candidate device). Treat
    # that as "nothing to record" — every caller already handles None.
    try:
        return await device_locking.lock_device_handle(db, device_id)
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


def _superseded_by_a_running_node(
    row: DesiredRow,
    at: str,
    observed_by_target: dict[str, ObservedEntry],
) -> bool:
    """True when the agent currently runs a node for *row* that started at or
    after the report — the report belongs to a superseded episode.

    Both timestamps are agent-minted (the failure ring and the running-node
    snapshot come from the same clock), so the comparison is meaningful.

    Target match only — deliberately no ``observed_by_port`` fallback, unlike
    the convergence path (``rows_needing_stale_clear``). The directions differ:
    there a mismatch strands a live node as ``observed_running`` False and the
    port fallback is the fail-safe recovery, while here a mismatch is fail-OPEN
    — a cross-device port collision would let another device's node declare
    this report superseded, advancing the cursor past a genuine
    ``port_conflict`` and skipping both the escalation and the ``desired_port``
    re-pin. Losing a real supersession only costs one extra escalation.
    """
    entry = match_observed_entry(row, observed_by_target)
    if entry is None or entry.started_at is None:
        return False
    try:
        reported_at = datetime.fromisoformat(at)
    except ValueError:
        return False
    if reported_at.tzinfo is None:
        reported_at = reported_at.replace(tzinfo=UTC)
    return reported_at <= entry.started_at


async def _repin_desired_port(
    db: AsyncSession, row: DesiredRow, *, conflict_port: int, settings: SettingsReader
) -> None:
    """Re-pin ``desired_port`` inside the caller's Device-locked transaction."""
    node = await lock_appium_node_for_device(db, row.device_id)
    if node is None:
        return
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
    # Ownership moves with the pin. ``node.port`` is what the agent's desired
    # spec is keyed on (``desired_port or port``) and what the intent
    # reconciler re-derives ``desired_port`` from on its next tick: leaving it
    # on the conflicted port makes the agent's own sweep reap the node it just
    # started on the new port, and lets the recompute undo this write ~5s
    # later. Both writers serialise on the Device row lock the caller holds.
    node.port = ports[0]
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


@dataclass(frozen=True, slots=True)
class ObservedNodeMutation:
    """One agent-observed node fact, detached from the session that read it."""

    state: str
    port: int | None
    pid: int | None
    details: NodeStartDetails
    clear_desired_port: bool


async def apply_observed_node_command(
    session_factory: SessionFactory,
    row: DesiredRow,
    mutation: ObservedNodeMutation,
    *,
    publisher: EventPublisher,
    settings: SettingsReader,
) -> None:
    """Settle one device's observed node state in its own short transaction.

    One Device lock, then the device-owned AppiumNode row; every nested helper
    only stages or flushes, so a failure rolls back this device alone and leaves
    successful peers durable.
    """
    async with session_factory.begin() as db:
        locked = await _lock_device_for_reconciler(db, row.device_id)
        if locked is None:
            return
        snapshot = await load_device_decision_snapshot(db, locked, now=now_utc())
        locked_node = await lock_appium_node_for_device(db, row.device_id)
        if mutation.state == "running":
            await mark_node_started(
                db,
                locked,
                locked_node,
                snapshot,
                port=mutation.port or row.port or 0,
                pid=mutation.pid,
                details=mutation.details,
                publisher=publisher,
                settings=settings,
            )
        else:
            await mark_node_stopped(
                db,
                locked,
                locked_node,
                snapshot,
                publisher=publisher,
            )
        if mutation.clear_desired_port and locked_node is not None:
            await write_desired_state(
                db,
                node=locked_node,
                caller="appium_reconciler",
                write=DesiredStateWrite(
                    target=locked_node.desired_state,
                    desired_port=None,
                    restart_requested_at=locked_node.restart_requested_at,
                ),
            )


async def _touch_last_observed(
    rows: list[DesiredRow], *, settings: SettingsReader, session_factory: SessionFactory
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
    async with session_factory.begin() as db:
        node_ids = [row.node_id for row in rows]
        await db.execute(update(AppiumNode).where(AppiumNode.id.in_(node_ids)).values(last_observed_at=now_utc()))


async def _record_start_failure(
    row: DesiredRow,
    *,
    reason: str,
    conflict_port: int | None = None,
    session_factory: SessionFactory,
    settings: SettingsReader,
) -> None:
    """Escalate one agent-reported start failure, re-pinning the port in the same
    Device-first transaction when the agent reported a port conflict."""
    async with session_factory.begin() as db:
        locked = await _lock_device_for_reconciler(db, row.device_id)
        if locked is None:
            return
        now = now_utc()
        snapshot = await load_device_decision_snapshot(db, locked, now=now)
        if conflict_port is not None:
            # Ahead of the backoff gate on purpose. The re-pin is not a ladder
            # action — it is the corrective write that makes the next start
            # attempt able to succeed — and it is idempotent under the Device
            # lock this transaction already holds. Behind the gate it was lost
            # whenever ANY backoff window was open, including one opened by a
            # different source (the ladder is shared), and a re-pin that itself
            # landed on a second occupied port stayed uncorrected until the
            # window expired — so a host leaking two ports could still walk the
            # device to review_required.
            await _repin_desired_port(db, row, conflict_port=conflict_port, settings=settings)
        if snapshot.ladder.backoff_active(now=now) is not None:
            # One escalation per failure episode. The agent keeps retrying (and
            # keeps reporting) on its own cadence while the backend's recovery
            # window is open; climbing a rung per report is what turned a single
            # transient port conflict into terminal review_required.
            logger.info(
                "appium_reconciler_start_failure_within_backoff",
                device_id=str(row.device_id),
                reason=reason,
            )
            return
        await escalate_device_remediation_failure(
            db,
            locked.device,
            settings=settings,
            source="appium_reconciler",
            reason=reason,
            ladder=snapshot.ladder,
        )


async def _reset_start_failure(
    row: DesiredRow,
    *,
    session_factory: SessionFactory,
    settings: SettingsReader,
) -> None:
    async with session_factory.begin() as db:
        locked = await _lock_device_for_reconciler(db, row.device_id)
        if locked is None:
            return
        snapshot = await load_device_decision_snapshot(db, locked, now=now_utc())
        await reset_reconciler_start_failure_if_needed(db, locked.device, ladder=snapshot.ladder)


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
                    pack_release=entry.pack_release,
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
                for row in sorted(stale_rows, key=lambda item: str(item.device_id)):
                    await clear_observed(row=row, state="stopped", port=None, pid=None, details=NodeStartDetails())
            # Start failures are observations, not decisions: fold them for
            # every row on the host, including rows in recovery backoff. Gating
            # ingestion on the ladder let the agent's ring accumulate a backlog
            # that landed as one rung per queued report when the window closed.
            await self._ingest_start_failure_reports(rows, raw_start_failures, observed=observed)
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
            await self.converge_host_rows(
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
        desired_rows: list[DesiredRow],
        observed: list[ObservedEntry],
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        raise_errors: bool = False,
    ) -> None:
        """Drive convergence for one host, one short transaction per device."""
        write_observed = self._write_observed_factory()
        reset_start_failure = self._make_reset_start_failure()
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
        *,
        observed: list[ObservedEntry] | None = None,
    ) -> None:
        """Ingest agent-reported start-failure facts for pull-only orchestration.

        Mark-stopped-on-absence is already handled by the
        ``db_clear_stale_running`` pass-through and is out of scope here.
        """
        await self._ingest_start_failures(rows, start_failures or [], observed or [])

    def _match_new_start_failure(
        self, failure: dict[str, Any], running_by_target: dict[str, DesiredRow]
    ) -> tuple[DesiredRow, str, object, str] | None:
        """Resolve one raw ``start_failures`` entry to ``(row, kind, port, at)``
        if it matches a desired-running row and is newer than the dedupe cursor
        for that device. Returns ``None`` for anything unmatched, malformed, or
        already-seen (level-style dedupe).

        Pure: the caller advances ``_last_seen_failure_at`` only once the entry's
        transaction has committed, so a failed command is redelivered instead of
        being silently swallowed.
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
        return row, kind, failure.get("port"), at

    async def _ingest_start_failures(
        self, rows: list[DesiredRow], start_failures: list[dict[str, Any]], observed: list[ObservedEntry]
    ) -> None:
        """Level-style ingest, collapsed to the newest ``start_failure`` per device.

        A ``port_conflict`` re-pins ``desired_port`` to the next free candidate
        and trips the existing start-failure backoff; a ``spawn_failed`` trips
        backoff only.

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
        # One action per device per ingestion window. The agent re-reports the
        # same failure on every ~5s poll, so a window can carry dozens of
        # entries for one device; only the newest describes the current state.
        newest: dict[uuid.UUID, tuple[DesiredRow, str, object, str]] = {}
        for failure in start_failures:
            matched = self._match_new_start_failure(failure, running_by_target)
            if matched is None:
                continue
            current = newest.get(matched[0].device_id)
            if current is None or matched[3] > current[3]:
                newest[matched[0].device_id] = matched
        observed_by_target = {entry.connection_target: entry for entry in observed}
        for device_id, (row, kind, port, at) in sorted(newest.items(), key=lambda item: str(item[0])):
            if _superseded_by_a_running_node(row, at, observed_by_target):
                # The node this report is about has already been replaced by one
                # that started later. Fold it so it stops being replayed, but do
                # not let it escalate or re-shelve the episode that succeeded.
                logger.info(
                    "appium_reconciler_start_failure_superseded",
                    device_id=str(device_id),
                    reason=kind,
                )
                self._last_seen_failure_at[device_id] = at
                continue
            if kind == "port_conflict":
                await _record_start_failure(
                    row,
                    reason="port_conflict",
                    conflict_port=port if isinstance(port, int) else None,
                    session_factory=self._session_factory,
                    settings=self._settings,
                )
            elif kind == "spawn_failed":
                await _record_start_failure(
                    row, reason="spawn_failed", session_factory=self._session_factory, settings=self._settings
                )
            # Cursor advances only here, after the command's transaction context
            # exited successfully: a raised command leaves it behind so the same
            # ring entry is retried on the next push.
            self._last_seen_failure_at[device_id] = at

    def _write_observed_factory(self) -> Callable[..., Awaitable[None]]:
        async def _write(
            *,
            row: DesiredRow,
            state: str,
            port: int | None,
            pid: int | None,
            details: NodeStartDetails | None = None,
            clear_desired_port: bool = False,
        ) -> None:
            await apply_observed_node_command(
                self._session_factory,
                row,
                ObservedNodeMutation(
                    state=state,
                    port=port,
                    pid=pid,
                    details=details or NodeStartDetails(),
                    clear_desired_port=clear_desired_port,
                ),
                publisher=self._publisher,
                settings=self._settings,
            )

        return _write

    def _make_reset_start_failure(self) -> Callable[..., Awaitable[None]]:
        async def _reset(*, row: DesiredRow) -> None:
            await _reset_start_failure(row, session_factory=self._session_factory, settings=self._settings)

        return _reset

    async def converge_device_now(self, device_id: uuid.UUID) -> None:
        """Wake the agent to run one desired-state convergence pass for a single
        operator-requested device.

        The periodic leader loop remains the durable fallback. This path only removes
        operator-visible latency after a route has already accepted and committed a
        desired-state change. Observe-only: no agent start/stop/restart I/O here,
        just a best-effort poke so the agent's own poller re-pulls desired state
        now — the next status push is what updates backend-observed state.
        """
        async with self._session_factory() as read_db:
            row = await _fetch_desired_row(read_db, device_id)
            if row is None:
                return
            host = await read_db.get(Host, row.host_id)
            if host is None or not host_online(
                host, offline_after_sec=self._settings.get_float("general.host_offline_after_sec")
            ):
                return
            target = NodeRefreshTarget(ip=host.ip, agent_port=host.agent_port)
        await poke_node_refresh_target(target, circuit_breaker=self._circuit_breaker, pool=self._pool)
