"""Leader-owned reconciler for agent-side Appium processes.

Drives desired-state convergence per online host: walks
`/agent/health.appium_processes.running_nodes`, reaps stray nodes that no
desired row can converge (see ``reconciler_convergence.reap_orphan_nodes``),
then starts/stops/restarts to match each device's desired AppiumNode state.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import httpx2 as httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import agent_base_url, agent_health
from app.agent_comm.snapshot import parse_running_nodes
from app.appium_nodes.exceptions import NodeAlreadyRunningError, NodeStopNotAcknowledgedError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import resource_service as appium_node_resource_service
from app.appium_nodes.services.desired_state_writer import DesiredStateWrite, write_desired_state
from app.appium_nodes.services.reconciler_agent import (
    NodeStartDetails,
    _start_for_node,
    mark_node_started,
    mark_node_stopped,
    stop_remote_node,
)
from app.appium_nodes.services.reconciler_convergence import (
    DesiredRow,
    ObservedEntry,
    _execute_action,
    _needs_start_failure_reset,
    decide_convergence_action,
    match_observed_entry,
    reap_orphan_nodes,
    rows_needing_stale_clear,
)
from app.core.background_loop import BackgroundLoop
from app.core.database import async_session
from app.core.leader.advisory import assert_current_leader
from app.core.metrics_recorders import (
    APPIUM_RECONCILER_CYCLE_FAILURES,
    APPIUM_RECONCILER_HOST_CYCLE_SECONDS,
    APPIUM_RECONCILER_LAST_CYCLE_SECONDS,
    APPIUM_RECONCILER_START_FAILURES,
    APPIUM_RECONCILER_STOP_FAILURES,
)
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device
from app.devices.services.lifecycle_policy_state import state as lifecycle_policy_state
from app.hosts.models import Host, HostStatus
from app.lifecycle.services.actions import (
    record_reconciler_start_failure_state,
    reset_reconciler_start_failure_state,
)

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)

if TYPE_CHECKING:
    SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]
else:
    SessionScope = Callable[[], object]


LOOP_NAME = "appium_reconciler"


class AppiumReconcilerLoop(BackgroundLoop):
    """Leader-owned periodic loop."""

    loop_name = LOOP_NAME
    exit_on_leadership_lost = True
    cycle_failed_message = "appium_reconciler_cycle_failed"

    def __init__(self, *, services: AppiumNodeServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _leadership_lost_event(self) -> str:
        return "appium_reconciler_leadership_lost"  # historical name: no "_loop" segment

    def _interval(self) -> float:
        return self._services.settings.get_float("appium_reconciler.interval_sec")

    async def _run_cycle(self, db: AsyncSession) -> None:
        await self._services.reconciler.run_cycle(db)

    def _on_cycle_end(self, elapsed_seconds: float, interval: float) -> None:
        APPIUM_RECONCILER_LAST_CYCLE_SECONDS.set(elapsed_seconds)

    def _on_cycle_error(self) -> None:
        APPIUM_RECONCILER_CYCLE_FAILURES.inc()


async def _fetch_online_hosts(db: AsyncSession) -> list[dict[str, object]]:
    result = await db.execute(select(Host.id, Host.ip, Host.agent_port).where(Host.status == HostStatus.online))
    return [{"id": row.id, "ip": row.ip, "agent_port": row.agent_port} for row in result.all()]


async def _fetch_desired_rows(db: AsyncSession) -> list[DesiredRow]:
    target_expr = func.coalesce(Device.connection_target, Device.identity_value)
    stmt = (
        select(
            Device.id.label("device_id"),
            Device.host_id,
            Device.lifecycle_policy_state,
            AppiumNode.id.label("node_id"),
            target_expr.label("connection_target"),
            AppiumNode.desired_state,
            AppiumNode.desired_port,
            AppiumNode.transition_token,
            AppiumNode.transition_deadline,
            AppiumNode.port,
            AppiumNode.pid,
            AppiumNode.active_connection_target,
            AppiumNode.stop_pending,
        )
        .join(AppiumNode, AppiumNode.device_id == Device.id)
        .join(Host, Host.id == Device.host_id)
        .where(Host.status == HostStatus.online)
    )
    rows = (await db.execute(stmt)).all()
    return [
        DesiredRow(
            device_id=row.device_id,
            host_id=row.host_id,
            node_id=row.node_id,
            connection_target=row.connection_target,
            desired_state=row.desired_state.value,
            desired_port=row.desired_port,
            transition_token=row.transition_token,
            transition_deadline=row.transition_deadline,
            port=row.port,
            pid=row.pid,
            active_connection_target=row.active_connection_target,
            stop_pending=row.stop_pending,
            lifecycle_policy_state=row.lifecycle_policy_state,
        )
        for row in rows
    ]


async def _fetch_desired_row(db: AsyncSession, device_id: uuid.UUID) -> DesiredRow | None:
    target_expr = func.coalesce(Device.connection_target, Device.identity_value)
    stmt = (
        select(
            Device.id.label("device_id"),
            Device.host_id,
            Device.lifecycle_policy_state,
            AppiumNode.id.label("node_id"),
            target_expr.label("connection_target"),
            AppiumNode.desired_state,
            AppiumNode.desired_port,
            AppiumNode.transition_token,
            AppiumNode.transition_deadline,
            AppiumNode.port,
            AppiumNode.pid,
            AppiumNode.active_connection_target,
            AppiumNode.stop_pending,
        )
        .join(AppiumNode, AppiumNode.device_id == Device.id)
        .where(Device.id == device_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None
    return DesiredRow(
        device_id=row.device_id,
        host_id=row.host_id,
        node_id=row.node_id,
        connection_target=row.connection_target,
        desired_state=row.desired_state.value,
        desired_port=row.desired_port,
        transition_token=row.transition_token,
        transition_deadline=row.transition_deadline,
        port=row.port,
        pid=row.pid,
        active_connection_target=row.active_connection_target,
        stop_pending=row.stop_pending,
        lifecycle_policy_state=row.lifecycle_policy_state,
    )


async def _fetch_backoff_until(db: AsyncSession) -> dict[uuid.UUID, datetime]:
    rows = (await db.execute(select(Device.id, Device.lifecycle_policy_state))).all()
    backoff: dict[uuid.UUID, datetime] = {}
    for device_id, state_json in rows:
        if not isinstance(state_json, dict):
            continue
        raw = state_json.get("backoff_until")
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            backoff[device_id] = parsed
        except TypeError, ValueError:
            continue
    return backoff


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


async def _clear_transition_token(db: AsyncSession, row: DesiredRow) -> None:
    device = await _lock_device_for_reconciler(db, row.device_id)
    if device is None or device.appium_node is None:
        return
    node = device.appium_node
    # ``transition_token_natural_clear`` keeps this expiry-driven clear out
    # of the ``APPIUM_TRANSITION_TOKEN_OVERRIDDEN`` metric: the old token
    # was not contended by a competing writer, the deadline elapsed.
    await write_desired_state(
        db,
        node=node,
        caller="appium_reconciler",
        write=DesiredStateWrite(
            target=node.desired_state,
            desired_port=node.desired_port,
            transition_token_natural_clear=True,
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
        await assert_current_leader(db, settings=settings)
        node_ids = [row.node_id for row in rows]
        await db.execute(update(AppiumNode).where(AppiumNode.id.in_(node_ids)).values(last_observed_at=now_utc()))
        await db.commit()


def _classify_start_failure(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError):
        text = exc.response.text.lower() if exc.response is not None else ""
        if "already_running" in text:
            return "already_running"
        if "port" in text or (exc.response is not None and exc.response.status_code == HTTPStatus.CONFLICT):
            return "port_occupied"
    if isinstance(exc, httpx.HTTPError):
        return "http_error"
    return "http_error"


async def _record_start_failure(
    row: DesiredRow,
    *,
    reason: str,
    require_leader: bool = True,
    session_scope: SessionScope | None = None,
    settings: SettingsReader,
) -> None:
    threshold = settings.get_int("appium_reconciler.start_failure_threshold")
    backoff_seconds = settings.get_int("appium.startup_timeout_sec") * 4
    resolved_session_scope = session_scope or async_session
    async with resolved_session_scope() as db:
        if require_leader:
            await assert_current_leader(db, settings=settings)
        device = await _lock_device_for_reconciler(db, row.device_id)
        if device is None:
            return
        current = lifecycle_policy_state(device)
        attempts = int(current.get("recovery_backoff_attempts", 0)) + 1
        backoff_until = None
        if attempts >= threshold:
            backoff_until = (now_utc() + timedelta(seconds=backoff_seconds)).isoformat()
        record_reconciler_start_failure_state(
            device,
            reason=reason,
            attempts=attempts,
            backoff_until=backoff_until,
        )
        await db.commit()


async def _reset_start_failure(
    row: DesiredRow,
    *,
    require_leader: bool = True,
    session_scope: SessionScope | None = None,
    settings: SettingsReader,
) -> None:
    resolved_session_scope = session_scope or async_session
    async with resolved_session_scope() as db:
        if require_leader:
            await assert_current_leader(db, settings=settings)
        device = await _lock_device_for_reconciler(db, row.device_id)
        if device is None:
            return
        current = lifecycle_policy_state(device)
        if not _needs_start_failure_reset(current):
            return
        reset_reconciler_start_failure_state(device)
        await db.commit()


class ReconcilerService:
    """Injectable service wrapping the Appium reconciler loop body.

    Satisfies :class:`~app.appium_nodes.protocols.ReconcilerProtocol`.
    The caller opens the initial DB session and passes it to :meth:`run_cycle`;
    agent IO and convergence passes open their own sessions via the stored
    session factory.
    """

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

    async def run_cycle(self, db: AsyncSession) -> None:
        """Execute one reconciler cycle using the provided DB session for reads.

        Agent IO and convergence passes open their own sessions internally.
        """
        await assert_current_leader(db, settings=self._settings)
        hosts = await _fetch_online_hosts(db)
        desired = await _fetch_desired_rows(db)
        backoff = await _fetch_backoff_until(db)
        # Agent IO and stops happen outside the DB session — no point holding it open.
        await self._drive_convergence(hosts, desired, backoff)

    async def _drive_convergence(
        self,
        hosts: list[dict[str, object]],
        desired: list[DesiredRow],
        backoff_until_by_device: dict[uuid.UUID, datetime],
        *,
        health_by_host: dict[uuid.UUID, dict[str, object]] | None = None,
        require_leader: bool = True,
    ) -> None:
        semaphore = asyncio.Semaphore(self._settings.get_int("appium_reconciler.host_parallelism"))
        now = now_utc()
        rows_by_host: dict[uuid.UUID, list[DesiredRow]] = {}
        active_rows_by_host: dict[uuid.UUID, list[DesiredRow]] = {}
        for row in desired:
            rows_by_host.setdefault(row.host_id, []).append(row)
            backoff_until = backoff_until_by_device.get(row.device_id)
            if backoff_until is not None and backoff_until > now:
                continue
            active_rows_by_host.setdefault(row.host_id, []).append(row)

        async def _reconcile_host(host: dict[str, object]) -> None:
            host_id = host.get("id")
            host_ip = host.get("ip")
            agent_port = host.get("agent_port")
            if not isinstance(host_id, uuid.UUID) or not isinstance(host_ip, str) or not isinstance(agent_port, int):
                return
            rows = rows_by_host.get(host_id, [])
            async with semaphore:
                cycle_start = time.monotonic()
                try:
                    async with self._session_factory() as db:
                        if require_leader:
                            await assert_current_leader(db, settings=self._settings)
                    payload = (health_by_host or {}).get(host_id)
                    if payload is None:
                        payload = (
                            await agent_health(
                                host_ip,
                                agent_port,
                                http_client_factory=httpx.AsyncClient,
                                settings=self._settings,
                                pool=self._pool,
                                circuit_breaker=self._circuit_breaker,
                            )
                            or {}
                        )
                    appium_processes = payload.get("appium_processes") if isinstance(payload, dict) else None
                    if not isinstance(appium_processes, dict):
                        return
                    running = parse_running_nodes(appium_processes)
                    observed = [
                        ObservedEntry(
                            port=entry.port,
                            pid=entry.pid,
                            connection_target=entry.connection_target,
                        )
                        for entry in running
                    ]
                    await _touch_last_observed(rows, settings=self._settings, session_factory=self._session_factory)
                    # Reap stray agent nodes (duplicates for one target, or nodes
                    # for a device not on this host) before convergence. Keyed off
                    # ALL host rows (rows), not the active subset, so a node for a
                    # device in recovery backoff is never mistaken for an orphan.
                    # Runs even when active_rows — or rows itself — is empty, so a
                    # row-less process on a host with no devices is still reaped.
                    await reap_orphan_nodes(observed, rows, stop_agent=self._make_stop_agent(host_ip, agent_port))
                    # Clear leaked observed pids for devices excluded from active
                    # convergence (in recovery backoff). The active loop below never
                    # reaches them, so a node stopped during backoff keeps a stale
                    # pid in the DB — which blocks an operator start ("node already
                    # running"). DB-only clear; never starts/stops an agent node.
                    backoff_rows = [
                        row
                        for row in rows
                        if (bu := backoff_until_by_device.get(row.device_id)) is not None and bu > now
                    ]
                    stale_rows = rows_needing_stale_clear(backoff_rows, observed, now=now)
                    if stale_rows:
                        clear_observed = self._write_observed_factory(require_leader=require_leader)
                        for row in stale_rows:
                            await clear_observed(
                                row=row, state="stopped", port=None, pid=None, active_connection_target=None
                            )
                    active_rows = active_rows_by_host.get(host_id, [])
                    if not active_rows:
                        return
                    await self.converge_host_rows(
                        None,
                        active_rows,
                        observed,
                        host_id=host_id,
                        host_ip=host_ip,
                        agent_port=agent_port,
                        require_leader=require_leader,
                    )
                finally:
                    APPIUM_RECONCILER_HOST_CYCLE_SECONDS.labels(host_id=str(host_id)).observe(
                        time.monotonic() - cycle_start
                    )

        await asyncio.gather(*(_reconcile_host(host) for host in hosts))

    async def converge_host_rows(
        self,
        db: AsyncSession | None,
        desired_rows: list[DesiredRow],
        observed: list[ObservedEntry],
        *,
        host_id: uuid.UUID,
        host_ip: str,
        agent_port: int,
        require_leader: bool = True,
        raise_errors: bool = False,
    ) -> None:
        """Drive convergence for one host."""
        session_scope = _session_scope(db)
        start_agent = self._make_start_agent(
            require_leader=require_leader,
            session_scope=session_scope,
        )
        stop_agent = self._make_stop_agent(host_ip, agent_port)
        write_observed = self._write_observed_factory(
            require_leader=require_leader,
            session_scope=session_scope,
        )
        clear_token = self._clear_token_factory(
            require_leader=require_leader,
            session_scope=session_scope,
        )
        reset_start_failure = self._make_reset_start_failure(
            require_leader=require_leader,
            session_scope=session_scope,
        )
        observed_by_target = {entry.connection_target: entry for entry in observed}
        for row in sorted(desired_rows, key=lambda item: str(item.device_id)):
            obs = match_observed_entry(row, observed_by_target)
            action = decide_convergence_action(row, observed=obs, now=now_utc())
            try:
                await _execute_action(
                    host_id=host_id,
                    row=row,
                    action=action,
                    start_agent=start_agent,
                    stop_agent=stop_agent,
                    write_observed=write_observed,
                    clear_token=clear_token,
                    reset_start_failure=reset_start_failure,
                )
            except NodeAlreadyRunningError, NodeStopNotAcknowledgedError:
                # Expected, self-healing transients during the Appium process
                # restart / sidecar-respawn window: a node already runs for the
                # target, or the agent hasn't acknowledged a stop yet. The next
                # reconciler tick converges; the APPIUM_RECONCILER_* metrics are
                # the durable signal, so log at debug, not warning.
                logger.debug(
                    "appium_reconciler_convergence_action_transient",
                    exc_info=True,
                    host_id=str(host_id),
                    device_id=str(row.device_id),
                    action=action.kind,
                )
                if raise_errors:
                    raise
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

    def _make_start_agent(
        self,
        *,
        require_leader: bool = True,
        session_scope: SessionScope | None = None,
    ) -> Callable[..., Awaitable[dict[str, Any]]]:
        resolved_session_scope = session_scope or self._session_factory

        async def _start(*, row: DesiredRow, port: int | None) -> dict[str, Any]:
            async with resolved_session_scope() as db:
                if require_leader:
                    await assert_current_leader(db, settings=self._settings)
                device = await _load_device_for_reconciler(db, row.device_id)
                if device is None:
                    raise RuntimeError(f"Device {row.device_id} no longer exists")
                try:
                    node = device.appium_node
                    if node is None:
                        raise RuntimeError(f"Device {row.device_id} has no AppiumNode row to converge")
                    handle = await _start_for_node(
                        db,
                        device,
                        node=node,
                        preferred_port=port,
                        settings=self._settings,
                        pool=self._pool,
                        circuit_breaker=self._circuit_breaker,
                    )
                    if handle.port <= 0:
                        raise RuntimeError(
                            f"Agent returned invalid Appium port {handle.port} for device {row.device_id}"
                        )
                except NodeAlreadyRunningError:
                    # The agent already runs a node for this target — not a start
                    # failure. Don't trip recovery backoff; let the convergence
                    # action treat it as already-converged.
                    raise
                except Exception as exc:
                    reason = _classify_start_failure(exc)
                    APPIUM_RECONCILER_START_FAILURES.labels(reason=reason).inc()
                    await _record_start_failure(
                        row,
                        reason=reason,
                        require_leader=require_leader,
                        session_scope=resolved_session_scope,
                        settings=self._settings,
                    )
                    raise
                await _reset_start_failure(
                    row,
                    require_leader=require_leader,
                    session_scope=resolved_session_scope,
                    settings=self._settings,
                )
                return {
                    "port": handle.port,
                    "pid": handle.pid,
                    "active_connection_target": handle.active_connection_target,
                    "allocated_caps": await appium_node_resource_service.get_capabilities(db, node_id=node.id),
                }

        return _start

    def _make_stop_agent(
        self,
        host_ip: str,
        agent_port: int,
    ) -> Callable[..., Awaitable[None]]:
        async def _stop(*, row: DesiredRow | None = None, port: int | None) -> None:
            if port is None or port <= 0:
                return
            try:
                stopped = await stop_remote_node(
                    port=port,
                    agent_base=agent_base_url(host_ip, agent_port),
                    host=host_ip,
                    agent_port=agent_port,
                    http_client_factory=httpx.AsyncClient,
                    settings=self._settings,
                    pool=self._pool,
                    circuit_breaker=self._circuit_breaker,
                )
            except Exception:
                APPIUM_RECONCILER_STOP_FAILURES.labels(reason="exception").inc()
                raise
            if not stopped:
                APPIUM_RECONCILER_STOP_FAILURES.labels(reason="not_acknowledged").inc()
                device_ref = row.device_id if row is not None else "<orphan>"
                raise NodeStopNotAcknowledgedError(
                    f"Agent did not acknowledge Appium stop for device {device_ref} on port {port}"
                )

        return _stop

    def _write_observed_factory(
        self,
        *,
        require_leader: bool = True,
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
            clear_desired_port: bool = False,
            clear_transition: bool = False,
            allocated_caps: object = None,
        ) -> None:
            async with resolved_session_scope() as db:
                if require_leader:
                    await assert_current_leader(db, settings=self._settings)
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
                            allocated_caps=allocated_caps if isinstance(allocated_caps, dict) else None,
                            clear_transition=clear_transition,
                        ),
                        publisher=self._publisher,
                        settings=self._settings,
                    )
                else:
                    await mark_node_stopped(db, device, publisher=self._publisher)
                # The running path delegates token clearing to mark_node_started
                # (clear_transition passed through), so it re-writes desired state
                # only to drop the port; the stopped path also clears the token here.
                if clear_desired_port or (state != "running" and clear_transition):
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
                            transition_token=None if clear_transition else node.transition_token,
                            transition_deadline=None if clear_transition else node.transition_deadline,
                        ),
                    )
                    await db.commit()

        return _write

    def _clear_token_factory(
        self,
        *,
        require_leader: bool = True,
        session_scope: SessionScope | None = None,
    ) -> Callable[..., Awaitable[None]]:
        resolved_session_scope = session_scope or self._session_factory

        async def _clear(*, row: DesiredRow) -> None:
            async with resolved_session_scope() as db:
                if require_leader:
                    await assert_current_leader(db, settings=self._settings)
                await _clear_transition_token(db, row)

        return _clear

    def _make_reset_start_failure(
        self,
        *,
        require_leader: bool = True,
        session_scope: SessionScope | None = None,
    ) -> Callable[..., Awaitable[None]]:
        async def _reset(*, row: DesiredRow) -> None:
            await _reset_start_failure(
                row, require_leader=require_leader, session_scope=session_scope, settings=self._settings
            )

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
            if host is None or host.status != HostStatus.online:
                return None

        payload = (
            await agent_health(
                host.ip,
                host.agent_port,
                http_client_factory=httpx.AsyncClient,
                settings=self._settings,
                pool=self._pool,
                circuit_breaker=self._circuit_breaker,
            )
            or {}
        )
        appium_processes = payload.get("appium_processes") if isinstance(payload, dict) else None
        if not isinstance(appium_processes, dict):
            return None
        observed = [
            ObservedEntry(
                port=entry.port,
                pid=entry.pid,
                connection_target=entry.connection_target,
            )
            for entry in parse_running_nodes(appium_processes)
        ]
        await self.converge_host_rows(
            db,
            [row],
            observed,
            host_id=host.id,
            host_ip=host.ip,
            agent_port=host.agent_port,
            require_leader=False,
            raise_errors=True,
        )
        async with session_scope() as read_db:
            node = await read_db.get(AppiumNode, row.node_id)
            if node is not None:
                await read_db.refresh(node)
            return node
