from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import agent_health
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.appium_nodes.services.common import node_state_severity
from app.appium_nodes.services.heartbeat_outcomes import (
    ClientMode,
    HeartbeatOutcome,
    HeartbeatPingResult,
)
from app.core.coerce import coerce_int as _coerce_int
from app.core.errors import AgentCallError, AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.core.leader import state_store as control_plane_state_store
from app.core.leader.advisory import control_plane_leader
from app.core.metrics_recorders import record_heartbeat_ping
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType
from app.devices.services.event import build_device_crashed_payload, record_event
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity import appium_connection_target
from app.hosts.models import Host, HostStatus
from app.hosts.service_status_push import HOST_STATUS_NAMESPACE

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher

logger = get_logger(__name__)


APPIUM_RESTART_SEQUENCE_NAMESPACE = "heartbeat.appium_restart_sequence"
APPIUM_RESTART_EVENT_KINDS = frozenset({"crash_detected", "restart_succeeded", "restart_exhausted"})
APPIUM_RESTART_EVENT_PROCESSES = frozenset({"appium"})


_TRANSPORT_TO_OUTCOME = {
    "timeout": HeartbeatOutcome.timeout,
    "connect_error": HeartbeatOutcome.connect_error,
    "dns_error": HeartbeatOutcome.dns_error,
}


def _heartbeat_client_mode(*, settings: SettingsReader) -> ClientMode:
    try:
        pool_enabled = bool(settings.get("agent.http_pool_enabled"))
        return ClientMode.pooled if pool_enabled else ClientMode.fresh
    except KeyError, RuntimeError:
        return ClientMode.fresh


async def _ping_agent(
    ip: str,
    port: int,
    *,
    settings: SettingsReader,
    pool: AgentHttpPool | None = None,
    circuit_breaker: CircuitBreakerProtocol,
) -> HeartbeatPingResult:
    started = time.monotonic()
    client_mode = _heartbeat_client_mode(settings=settings)
    try:
        payload = await agent_health(
            ip,
            port,
            http_client_factory=httpx.AsyncClient,
            settings=settings,
            pool=pool,
            circuit_breaker=circuit_breaker,
        )
    except CircuitOpenError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return HeartbeatPingResult(
            outcome=HeartbeatOutcome.circuit_open,
            payload=None,
            duration_ms=duration_ms,
            client_mode=ClientMode.skipped_circuit_open,
            http_status=None,
            error_category=type(exc).__name__,
        )
    except AgentResponseError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return HeartbeatPingResult(
            outcome=HeartbeatOutcome.http_error,
            payload=None,
            duration_ms=duration_ms,
            client_mode=client_mode,
            http_status=exc.http_status,
            error_category=type(exc).__name__,
        )
    except AgentUnreachableError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        outcome = _TRANSPORT_TO_OUTCOME.get(
            exc.transport_outcome or "",
            HeartbeatOutcome.unexpected_error,
        )
        return HeartbeatPingResult(
            outcome=outcome,
            payload=None,
            duration_ms=duration_ms,
            client_mode=client_mode,
            http_status=None,
            error_category=exc.error_category or type(exc).__name__,
        )
    except AgentCallError as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        return HeartbeatPingResult(
            outcome=HeartbeatOutcome.unexpected_error,
            payload=None,
            duration_ms=duration_ms,
            client_mode=client_mode,
            http_status=None,
            error_category=type(exc).__name__,
        )
    duration_ms = int((time.monotonic() - started) * 1000)
    if payload is None:
        return HeartbeatPingResult(
            outcome=HeartbeatOutcome.invalid_payload,
            payload=None,
            duration_ms=duration_ms,
            client_mode=client_mode,
            http_status=None,
            error_category=None,
        )
    return HeartbeatPingResult(
        outcome=HeartbeatOutcome.success,
        payload=payload,
        duration_ms=duration_ms,
        client_mode=client_mode,
        http_status=200,
        error_category=None,
    )


def _emit_heartbeat_log(
    *,
    host_id: str,
    host_ip: str,
    agent_port: int,
    result: HeartbeatPingResult,
    leader_id: str,
    loop_iteration: int,
) -> None:
    logger.info(
        "heartbeat_ping",
        host_id=host_id,
        host_ip=host_ip,
        agent_port=agent_port,
        client_mode=result.client_mode.value,
        duration_ms=result.duration_ms,
        outcome=result.outcome.value,
        http_status=result.http_status,
        error_category=result.error_category,
        leader_id=leader_id,
        loop_iteration=loop_iteration,
    )


def _restart_process(value: object) -> str:
    if isinstance(value, str) and value in APPIUM_RESTART_EVENT_PROCESSES:
        return value
    return "appium"


def _restart_error_message(kind: str, exit_code: int | None) -> str:
    exit_detail = f" (code {exit_code})" if exit_code is not None else ""
    if kind == "restart_exhausted":
        return f"Agent auto-restart exhausted after Appium exit{exit_detail}"
    return f"Agent detected Appium exit{exit_detail}"


def _restart_event_observation_changed(
    locked: AppiumNode,
    *,
    observed_id: uuid.UUID,
    observed_port: int,
    observed_pid: int | None,
    observed_active_connection_target: str | None,
) -> bool:
    return (
        locked.id != observed_id
        or locked.port != observed_port
        or locked.pid != observed_pid
        or locked.active_connection_target != observed_active_connection_target
    )


def _collect_restart_candidate_events(raw_events: list[Any], *, last_sequence: int) -> list[dict[str, Any]]:
    candidate_events: list[dict[str, Any]] = []
    for raw_event in raw_events:
        if not isinstance(raw_event, dict):
            continue
        sequence = _coerce_int(raw_event.get("sequence"))
        port = _coerce_int(raw_event.get("port"))
        kind = raw_event.get("kind")
        if sequence is None or sequence <= last_sequence or port is None:
            continue
        if not isinstance(kind, str) or kind not in APPIUM_RESTART_EVENT_KINDS:
            continue
        candidate_events.append(raw_event)
    return candidate_events


@dataclass(frozen=True, slots=True)
class _RestartEventFields:
    sequence: int
    process: str
    kind: str
    attempt: int
    port: int
    will_retry: bool
    delay_sec: int | None
    exit_code: int | None
    pid: int | None


def _build_restart_event_details(event: dict[str, Any], fields: _RestartEventFields) -> dict[str, Any]:
    details: dict[str, Any] = {
        "source": "agent_local_restart",
        "sequence": fields.sequence,
        "process": fields.process,
        "kind": fields.kind,
        "attempt": fields.attempt,
        "port": fields.port,
        "will_restart": fields.will_retry,
    }
    if fields.delay_sec is not None:
        details["delay_sec"] = fields.delay_sec
    if fields.exit_code is not None:
        details["exit_code"] = fields.exit_code
    if fields.pid is not None:
        details["pid"] = fields.pid
    occurred_at = event.get("occurred_at")
    if isinstance(occurred_at, str):
        details["occurred_at"] = occurred_at
    return details


async def _handle_restart_succeeded(
    db: AsyncSession,
    device: Device,
    locked_node: AppiumNode,
    *,
    process: str,
    pid: int | None,
    port: int,
    details: dict[str, Any],
    publisher: EventPublisher,
) -> None:
    if process == "appium" and pid is not None:
        locked_node.pid = pid
        # Eager-fill the node-viability marker (I11/N15). A reconciler poll that
        # observed the crash window may have nulled active_connection_target; the
        # agent has now confirmed the node is back, so restore it immediately rather
        # than waiting for the next reconciler poll to refill it — otherwise the
        # device reads ``available`` but fails the allocator's node_viable_predicate
        # (pid + active_connection_target set, no unsatisfied restart watermark) for up to one
        # reconciler interval. The value is a liveness marker only (routing uses
        # host.ip + node.port via node_target); the reconciler reconciles it to the
        # agent-reported connection_target on its next poll. Only fill when null so a
        # live value the reconciler already wrote is never churned.
        if locked_node.active_connection_target is None:
            locked_node.active_connection_target = appium_connection_target(device)
    locked_node.consecutive_health_failures = 0
    if process == "appium":
        publisher.queue_for_session(
            db,
            "node.state_changed",
            {
                "device_id": str(device.id),
                "device_name": device.name,
                "old_state": "error",
                "new_state": "running",
                "port": port,
            },
            severity=node_state_severity("error", "running"),
        )
    await record_event(
        db,
        device.id,
        DeviceEventType.node_restart,
        {
            **details,
            "recovered_from": "agent_auto_restart",
        },
    )
    await DeviceHealthService(publisher=publisher).apply_node_state_transition(
        db,
        device,
        health_running=None,
        health_state=None,
        mark_offline=False,
    )


async def _handle_restart_failure(
    db: AsyncSession,
    device: Device,
    *,
    kind: str,
    exit_code: int | None,
    process: str,
    will_retry: bool,
    details: dict[str, Any],
    publisher: EventPublisher,
) -> None:
    error_message = _restart_error_message(kind, exit_code)
    publisher.queue_for_session(
        db,
        "node.crash",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "error": error_message,
            "will_restart": will_retry,
            "process": process,
        },
        severity="warning" if will_retry else None,
    )
    publisher.queue_for_session(
        db,
        "device.crashed",
        build_device_crashed_payload(
            device_id=str(device.id),
            device_name=device.name,
            source="agent_restart_exhausted" if kind == "restart_exhausted" else "appium_crash",
            reason=error_message,
            will_restart=will_retry,
            process=process,
        ),
        severity="warning" if will_retry else None,
    )
    await record_event(
        db,
        device.id,
        DeviceEventType.node_crash,
        {
            **details,
            "error": error_message,
        },
    )
    degraded_state = "restart_exhausted" if kind == "restart_exhausted" else "restarting"
    await DeviceHealthService(publisher=publisher).apply_node_state_transition(
        db,
        device,
        health_running=False,
        health_state=degraded_state,
        mark_offline=False,
    )


async def _ingest_appium_restart_events(
    db: AsyncSession, host: Host, health_data: dict[str, Any], *, publisher: EventPublisher
) -> None:
    process_payload = health_data.get("appium_processes")
    if not isinstance(process_payload, dict):
        return

    raw_events = process_payload.get("recent_restart_events")
    if not isinstance(raw_events, list) or not raw_events:
        return

    host_key = str(host.id)
    last_sequence = (
        _coerce_int(await control_plane_state_store.get_value(db, APPIUM_RESTART_SEQUENCE_NAMESPACE, host_key)) or 0
    )

    candidate_events = _collect_restart_candidate_events(raw_events, last_sequence=last_sequence)

    if not candidate_events:
        return

    candidate_events.sort(key=lambda event: _coerce_int(event.get("sequence")) or 0)
    ports = sorted({port for event in candidate_events if (port := _coerce_int(event.get("port"))) is not None})
    node_stmt = (
        select(AppiumNode)
        .join(Device)
        .where(Device.host_id == host.id, AppiumNode.port.in_(ports))
        .options(selectinload(AppiumNode.device))
    )
    node_result = await db.execute(node_stmt)
    nodes_by_port = {node.port: node for node in node_result.scalars().all()}

    highest_sequence = last_sequence
    for event in candidate_events:
        sequence = _coerce_int(event.get("sequence"))
        port = _coerce_int(event.get("port"))
        if sequence is None or port is None:
            continue
        highest_sequence = max(highest_sequence, sequence)
        node = nodes_by_port.get(port)
        if node is None:
            continue
        observed_id = node.id
        observed_port = node.port
        observed_pid = node.pid
        observed_active_connection_target = node.active_connection_target
        # Acquire Device → AppiumNode locks before mutating node state.
        device_id = node.device.id
        locked_device = await device_locking.lock_device(db, device_id)
        locked_node = await appium_node_locking.lock_appium_node_for_device(db, device_id)
        if locked_node is None:
            continue
        if _restart_event_observation_changed(
            locked_node,
            observed_id=observed_id,
            observed_port=observed_port,
            observed_pid=observed_pid,
            observed_active_connection_target=observed_active_connection_target,
        ):
            logger.info(
                "Skipping stale local restart event for host %s port %s after node changed",
                host.hostname,
                port,
            )
            continue
        device = locked_device
        kind = str(event["kind"])
        process = _restart_process(event.get("process"))
        attempt = _coerce_int(event.get("attempt")) or 0
        delay_sec = _coerce_int(event.get("delay_sec"))
        exit_code = _coerce_int(event.get("exit_code"))
        pid = _coerce_int(event.get("pid"))
        will_retry = bool(event.get("will_retry"))

        details = _build_restart_event_details(
            event,
            _RestartEventFields(
                sequence=sequence,
                process=process,
                kind=kind,
                attempt=attempt,
                port=port,
                will_retry=will_retry,
                delay_sec=delay_sec,
                exit_code=exit_code,
                pid=pid,
            ),
        )

        if kind == "restart_succeeded":
            await _handle_restart_succeeded(
                db,
                device,
                locked_node,
                process=process,
                pid=pid,
                port=port,
                details=details,
                publisher=publisher,
            )
            continue

        await _handle_restart_failure(
            db,
            device,
            kind=kind,
            exit_code=exit_code,
            process=process,
            will_retry=will_retry,
            details=details,
            publisher=publisher,
        )

    await control_plane_state_store.set_value(db, APPIUM_RESTART_SEQUENCE_NAMESPACE, host_key, highest_sequence)


@dataclass(frozen=True, slots=True)
class _ResumeGuard:
    active: bool
    gap_sec: float | None = None
    threshold_sec: float | None = None


@dataclass(frozen=True, slots=True)
class HostStatusEvaluation:
    alive: bool
    payload: dict[str, Any] | None
    stale_for_sec: float


class HeartbeatService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        pool: AgentHttpPool,
        circuit_breaker: CircuitBreakerProtocol,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._pool = pool
        self._circuit_breaker = circuit_breaker
        self._session_factory = session_factory
        self._loop_iteration = 0
        self._last_cycle_monotonic: float | None = None

    def _next_loop_iteration(self) -> int:
        self._loop_iteration += 1
        return self._loop_iteration

    def begin_cycle(self) -> _ResumeGuard:
        """Start one sweep cycle and compute its suspend/resume guard."""
        self._next_loop_iteration()
        threshold = self._settings.get_float("general.host_offline_after_sec")
        now_mono = time.monotonic()
        prev_mono = self._last_cycle_monotonic
        # First cycle after process start is always guarded: last_heartbeat may
        # be stale from before the restart while agents are still (re)pushing.
        guard_active = prev_mono is None or (now_mono - prev_mono) > threshold
        self._last_cycle_monotonic = now_mono
        gap = round(now_mono - prev_mono, 1) if prev_mono is not None else None
        return _ResumeGuard(active=guard_active, gap_sec=gap, threshold_sec=threshold)

    async def evaluate_host(self, db: AsyncSession, host: Host, *, guard: _ResumeGuard) -> HostStatusEvaluation:
        """Recency verdict from the latest status push; the caller owns the commit."""
        offline_after = self._settings.get_float("general.host_offline_after_sec")
        reference = host.last_heartbeat or host.created_at  # enrollment grace for never-pushed hosts
        stale_for = (now_utc() - reference).total_seconds()

        if stale_for <= offline_after:
            raw = await control_plane_state_store.get_value(db, HOST_STATUS_NAMESPACE, str(host.id))
            payload = raw.get("payload") if isinstance(raw, dict) else None
            if isinstance(payload, dict):
                await _ingest_appium_restart_events(db, host, payload, publisher=self._publisher)
            else:
                payload = None
            return HostStatusEvaluation(alive=True, payload=payload, stale_for_sec=stale_for)

        if guard.active:
            logger.warning(
                "host_liveness_resume_guard_swallowed",
                host_id=str(host.id),
                gap_sec=guard.gap_sec,
                threshold_sec=guard.threshold_sec,
                stale_for_sec=round(stale_for, 1),
            )
            return HostStatusEvaluation(alive=False, payload=None, stale_for_sec=stale_for)

        if host.status == HostStatus.online:
            logger.error("Host %s marked offline: no status push for %.0fs", host.hostname, stale_for)
            publisher = self._publisher
            publisher.queue_for_session(
                db,
                "host.status_changed",
                {
                    "host_id": str(host.id),
                    "hostname": host.hostname,
                    "old_status": host.status.value,
                    "new_status": "offline",
                },
            )
            publisher.queue_for_session(
                db,
                "host.heartbeat_lost",
                {
                    "host_id": str(host.id),
                    "hostname": host.hostname,
                    "stale_for_sec": round(stale_for, 1),
                    "last_push_at": host.last_heartbeat.isoformat() if host.last_heartbeat else None,
                },
            )
            host.status = HostStatus.offline
            # Mark all devices on this host as offline. lock_devices
            # acquires SELECT FOR UPDATE on each row in id order so
            # operational_state writes serialize against concurrent writers.
            device_id_stmt = select(Device.id).where(Device.host_id == host.id)
            device_ids = list((await db.execute(device_id_stmt)).scalars().all())
            for device in await device_locking.lock_devices(db, device_ids):
                await record_event(
                    db,
                    device.id,
                    DeviceEventType.connectivity_lost,
                    {"reason": f"Host {host.hostname} offline", "host_id": str(host.id)},
                )
                await DeviceHealthService(publisher=publisher).update_device_checks(
                    db,
                    device,
                    healthy=False,
                    summary=f"Host {host.hostname} offline",
                )
        return HostStatusEvaluation(alive=False, payload=None, stale_for_sec=stale_for)

    async def probe_host(self, *, host_id: str, host_ip: str, agent_port: int) -> HeartbeatPingResult:
        """Network-partition diagnostic: can the backend reach the agent it and
        the router must dial directly? No state ingestion, no DB writes."""
        result = await _ping_agent(
            host_ip, agent_port, settings=self._settings, pool=self._pool, circuit_breaker=self._circuit_breaker
        )
        _emit_heartbeat_log(
            host_id=host_id,
            host_ip=host_ip,
            agent_port=agent_port,
            result=result,
            leader_id=str(control_plane_leader.holder_id),
            loop_iteration=self._loop_iteration,
        )
        record_heartbeat_ping(
            host_id=host_id,
            outcome=result.outcome.value,
            client_mode=result.client_mode.value,
            duration_seconds=result.duration_ms / 1000.0,
        )
        if result.outcome is not HeartbeatOutcome.success:
            logger.warning("agent_partition_suspected", host_id=host_id, host_ip=host_ip, outcome=result.outcome.value)
        return result
