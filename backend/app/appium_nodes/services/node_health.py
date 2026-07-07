from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import TYPE_CHECKING

import httpx2 as httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import appium_status as fetch_appium_status
from app.agent_comm.probe_result import ProbeResult, from_status_response
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.appium_nodes.services.common import node_state_severity
from app.appium_nodes.services.reconciler_agent import require_management_host
from app.core.background_loop import BackgroundLoop
from app.core.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.core.leader.advisory import assert_current_leader
from app.core.metrics_recorders import record_background_loop_phase
from app.core.observability import get_logger
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceEventType
from app.devices.schemas.device import DeviceLifecyclePolicySummaryState
from app.devices.services.event import record_event
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import (
    NODE_PROCESS,
    PRIORITY_AUTO_RECOVERY,
    RECOVERY,
    IntentRegistration,
)
from app.lifecycle.services.incidents import LifecycleIncidentDetails

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.protocols import DeviceNodeHealthWriter, DeviceRecoveryControl
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.core.type_defs import SessionFactory
    from app.events.protocols import EventPublisher
    from app.lifecycle.services.incidents import LifecycleIncidentService

logger = get_logger(__name__)
LOOP_NAME = "node_health"


@dataclass(frozen=True)
class NodeHealthCheckRequest:
    node: AppiumNode
    device: Device
    observed_port: int
    observed_pid: int | None
    observed_active_connection_target: str | None


class NodeHealthService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        pool: AgentHttpPool,
        circuit_breaker: CircuitBreakerProtocol,
        recovery_control: DeviceRecoveryControl,
        health: DeviceNodeHealthWriter,
        incidents: LifecycleIncidentService,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._pool = pool
        self._circuit_breaker = circuit_breaker
        self._recovery_control = recovery_control
        self._health = health
        self._incidents = incidents

    async def check_nodes(self, db: AsyncSession) -> None:
        stmt = (
            select(AppiumNode)
            .where(
                AppiumNode.pid.is_not(None),
                AppiumNode.active_connection_target.is_not(None),
                # Don't probe nodes we are intentionally stopping (I1); a refused
                # probe during teardown is expected, not a health failure. The
                # locked re-check in _process_node_health covers the race where
                # desired_state flips to stopped during the probe gather.
                AppiumNode.desired_state == AppiumDesiredState.running,
            )
            .options(
                selectinload(AppiumNode.device).selectinload(Device.host),
                selectinload(AppiumNode.device).selectinload(Device.appium_node),
            )
            .order_by(AppiumNode.device_id)
        )
        node_result = await db.execute(stmt)
        nodes = node_result.scalars().all()

        requests = [
            NodeHealthCheckRequest(
                node=node,
                device=node.device,
                observed_port=node.port,
                observed_pid=node.pid,
                observed_active_connection_target=node.active_connection_target,
            )
            for node in nodes
        ]
        probe_concurrency = self._settings.get_int("general.probe_concurrency_per_host")
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(probe_concurrency)
        )
        probe_started = perf_counter()
        results = await asyncio.gather(
            *[
                self._bounded_check_node_health(
                    host_semaphores[request.device.host_id],
                    request.node,
                    request.device,
                )
                for request in requests
            ]
        )
        record_background_loop_phase(LOOP_NAME, "probe", perf_counter() - probe_started)

        apply_started = perf_counter()
        # Fence: probes (asyncio.gather above) are slow external calls. If
        # another backend took leadership while we awaited them, drop all
        # writes from this cycle.
        await assert_current_leader(db, settings=self._settings)

        for request, result in zip(requests, results, strict=True):
            try:
                locked_device = await device_locking.lock_device(db, request.device.id, load_sessions=True)
            except NoResultFound:
                logger.warning(
                    "Node health check skipped: device %s no longer exists",
                    request.device.id,
                )
                await db.commit()
                continue

            await self._process_node_health(
                db,
                request.node,
                locked_device,
                result=result,
                observed_port=request.observed_port,
                observed_pid=request.observed_pid,
                observed_active_connection_target=request.observed_active_connection_target,
            )
            await db.commit()
        record_background_loop_phase(LOOP_NAME, "apply", perf_counter() - apply_started)

    async def _bounded_check_node_health(
        self,
        semaphore: asyncio.Semaphore,
        node: AppiumNode,
        device: Device,
    ) -> ProbeResult:
        async with semaphore:
            return await self._check_node_health(node, device)

    async def _check_node_health(
        self,
        node: AppiumNode,
        device: Device,
    ) -> ProbeResult:
        try:
            host = require_management_host(device, action="monitor Appium node health")
        except NodeManagerError:
            return ProbeResult(status="refused", detail="no management host")

        try:
            payload = await fetch_appium_status(
                host.ip,
                host.agent_port,
                node.port,
                http_client_factory=httpx.AsyncClient,
                settings=self._settings,
                pool=self._pool,
                circuit_breaker=self._circuit_breaker,
            )
            return from_status_response(payload)
        except AgentUnreachableError, AgentResponseError, CircuitOpenError:
            return ProbeResult(status="indeterminate", detail="agent transport error")

    async def _attempt_node_restart(self, db: AsyncSession, *, device: Device) -> None:
        node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one_or_none()
        if node is None:
            return
        window_sec = self._settings.get_int("appium_reconciler.restart_window_sec")
        deadline = now_utc() + timedelta(seconds=window_sec)
        # TTL (transition deadline) replaces the node_running precondition (semantic
        # delta #1): the start row self-expires; baseline:idle sustains a running node.
        await IntentService(db).register_intents_and_reconcile(
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=f"auto_recovery:node:{device.id}",
                    axis=NODE_PROCESS,
                    payload={
                        "action": "start",
                        "priority": PRIORITY_AUTO_RECOVERY,
                        "transition_token": str(uuid.uuid4()),
                        "transition_deadline": deadline.isoformat(),
                    },
                    expires_at=deadline,
                ),
                IntentRegistration(
                    source=f"auto_recovery:recovery:{device.id}",
                    axis=RECOVERY,
                    payload={"allowed": True, "priority": PRIORITY_AUTO_RECOVERY, "reason": "Node health restart"},
                    expires_at=deadline,
                ),
            ],
            publisher=self._publisher,
        )
        await db.commit()

    async def _process_node_health(
        self,
        db: AsyncSession,
        node: AppiumNode,
        device: Device,
        *,
        result: ProbeResult,
        observed_port: int | None = None,
        observed_pid: int | None = None,
        observed_active_connection_target: str | None = None,
    ) -> None:
        locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
        if locked_node is None:
            # Node was deleted between the caller's lock_device and here. Bail out
            # quietly; lower layers will reconcile on the next sweep.
            return

        if observed_port is not None and (
            locked_node.port != observed_port
            or locked_node.pid != observed_pid
            or locked_node.active_connection_target != observed_active_connection_target
        ):
            logger.info(
                "Node health check for device %s skipped stale probe result after node changed",
                device.name,
            )
            return

        node = locked_node

        if locked_node.pid is None or locked_node.active_connection_target is None:
            return

        if locked_node.desired_state == AppiumDesiredState.stopped:
            # Intentional-stop veto (I1): the node is being torn down on purpose
            # (desired_state=stopped) but is still observed_running until the stop
            # propagates. A refused/failed probe in that window is EXPECTED
            # teardown, not a health failure — counting it would escalate an
            # auto-recovery restart that fights the stop. The reconciler owns
            # driving it to stopped. Re-checked here under the row lock because
            # desired_state can flip to stopped during the async probe gather,
            # after check_nodes' (unlocked) SELECT filtered on it.
            return

        if result.status == "indeterminate":
            # Indeterminate-probe veto: a network error against the agent is
            # inconclusive evidence. Never count it as a failure and never
            # drive recovery from it — only positive probe evidence
            # (``ack``/``refused``) moves the node's health state.
            return

        healthy = result.status == "ack"

        if healthy:
            if locked_node.consecutive_health_failures > 0:
                logger.info("Node for device %s (%s) recovered", device.name, device.identity_value)
                await self._recovery_control.record_control_action(
                    db,
                    device,
                    action="node_monitor_recovered",
                    failure_source="node_health",
                    failure_reason="Node health checks recovered",
                )
                # The dedicated ``lifecycle_recovered`` incident below already
                # describes the recovery; pass ``record_incident=False`` to avoid
                # publishing a duplicate ``lifecycle_recovered`` event for the
                # same recovery moment.
                await self._recovery_control.clear_pending_auto_stop_on_recovery(
                    db,
                    device,
                    source="node_health",
                    reason="Node health checks recovered",
                    record_incident=False,
                )
                self._publisher.queue_for_session(
                    db,
                    "node.state_changed",
                    {
                        "device_id": str(device.id),
                        "device_name": device.name,
                        "old_state": "error",
                        "new_state": "running",
                        "port": node.port,
                    },
                    severity=node_state_severity("error", "running"),
                )
                await record_event(
                    db,
                    device.id,
                    DeviceEventType.node_restart,
                    {"recovered_from": "health_check_failure", "port": node.port},
                )
                await self._incidents.record_lifecycle_incident(
                    db,
                    device,
                    DeviceEventType.lifecycle_recovered,
                    LifecycleIncidentDetails(
                        summary_state=DeviceLifecyclePolicySummaryState.idle,
                        reason="Node health checks recovered",
                        detail="The node resumed healthy operation after transient failures",
                        source="node_health",
                    ),
                )
            locked_node.consecutive_health_failures = 0
            # Direct probe acked: the node is the authoritative health signal.
            # Persist the positive result truthfully — health_running=True —
            # instead of clearing the columns to NULL and relying on the
            # pid-based fallback in node_running_signal. ``health_state`` is
            # cleared so the public summary label stays "running" rather than
            # echoing an "error" stamp.
            await self._health.apply_node_state_transition(
                db,
                device,
                health_running=True,
                health_state=None,
                mark_offline=False,
            )
            return

        await self._record_health_failure(db, node, locked_node, device)

    async def _record_health_failure(
        self,
        db: AsyncSession,
        node: AppiumNode,
        locked_node: AppiumNode,
        device: Device,
    ) -> None:
        locked_node.consecutive_health_failures += 1
        count = locked_node.consecutive_health_failures
        max_failures = self._settings.get("general.node_max_failures")
        await self._health.apply_node_state_transition(
            db,
            device,
            health_running=False,
            health_state="error",
            mark_offline=count >= max_failures,
        )
        logger.warning(
            "Node health check failed for device %s (port %d): %d/%d",
            device.name,
            node.port,
            count,
            max_failures,
        )
        await record_event(
            db,
            device.id,
            DeviceEventType.health_check_fail,
            {"consecutive_failures": count, "port": node.port},
        )

        if count >= max_failures:
            locked_node.consecutive_health_failures = 0

            logger.error("Node for device %s reached max failures, attempting restart", device.name)
            await self._attempt_node_restart(db, device=device)


class NodeHealthLoop(BackgroundLoop):
    """Background loop that checks Appium node health.

    Polls Appium node health on the registry-configured interval, running
    as a drift reconciler.
    """

    loop_name = LOOP_NAME
    exit_on_leadership_lost = True
    cycle_failed_message = "Node health check failed"

    def __init__(self, *, services: AppiumNodeServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return self._services.settings.get_float("general.node_check_interval_sec")

    async def _run_cycle(self, db: AsyncSession) -> None:
        await self._services.node_health.check_nodes(db)
