from __future__ import annotations

import asyncio
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from prometheus_client import Counter
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import appium_status as fetch_appium_status
from app.agent_comm.probe_result import ProbeResult, from_status_response
from app.appium_nodes.exceptions import NodeManagerError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import locking as appium_node_locking
from app.appium_nodes.services.common import node_state_severity
from app.appium_nodes.services.reconciler_agent import require_management_host
from app.core.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.core.leader.advisory import LeadershipLost, assert_current_leader
from app.core.observability import get_logger, observe_background_loop
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
    NodeRunningPrecondition,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_comm.http_pool import AgentHttpPool
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.protocols import DeviceNodeHealthWriter, DeviceRecoveryControl, LifecycleIncidentRecorder
    from app.appium_nodes.services_container import AppiumNodeServices
    from app.core.protocols import SettingsReader
    from app.events.protocols import EventPublisher
    from app.grid.protocols import GridServiceProtocol

logger = get_logger(__name__)
LOOP_NAME = "node_health"
PROBE_CONCURRENCY_PER_HOST = 2

NODE_HEALTH_WAKE_SOURCE_TOTAL = Counter(
    "gridfleet_node_health_wake_source",
    "Why node_health_loop ran a cycle: doorbell (bus event) or tick (timeout).",
    labelnames=("source",),
)


@dataclass(frozen=True)
class NodeHealthCheckRequest:
    node: AppiumNode
    device: Device
    observed_port: int
    observed_pid: int | None
    observed_active_connection_target: str | None


def _grid_registration_grace_active(node: AppiumNode, *, settings: SettingsReader) -> bool:
    started_at = node.started_at
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - started_at).total_seconds()
    return 0 <= age_seconds < int(settings.get("appium.startup_timeout_sec"))


class NodeHealthService:
    def __init__(
        self,
        *,
        publisher: EventPublisher,
        settings: SettingsReader,
        pool: AgentHttpPool,
        circuit_breaker: CircuitBreakerProtocol,
        grid: GridServiceProtocol,
        recovery_control: DeviceRecoveryControl,
        health: DeviceNodeHealthWriter,
        incidents: LifecycleIncidentRecorder,
    ) -> None:
        self._publisher = publisher
        self._settings = settings
        self._pool = pool
        self._circuit_breaker = circuit_breaker
        self._grid = grid
        self._recovery_control = recovery_control
        self._health = health
        self._incidents = incidents
        self._doorbell: asyncio.Event | None = None  # lazy: created on first access on the running loop

    def _get_doorbell(self) -> asyncio.Event:
        if self._doorbell is None:
            self._doorbell = asyncio.Event()
        return self._doorbell

    def wake(self) -> None:
        self._get_doorbell().set()

    async def wait_for_wake(self, timeout: float) -> bool:
        """Wait for a doorbell wake or timeout; clear and report which fired.

        Returns True if doorbell-woken, False on timeout.
        """
        doorbell = self._get_doorbell()
        try:
            await asyncio.wait_for(doorbell.wait(), timeout=timeout)
            woke = True
        except TimeoutError:
            woke = False
        doorbell.clear()
        return woke

    async def check_nodes(self, db: AsyncSession) -> None:
        stmt = (
            select(AppiumNode)
            .where(AppiumNode.pid.is_not(None), AppiumNode.active_connection_target.is_not(None))
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
        host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(PROBE_CONCURRENCY_PER_HOST)
        )
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
        grid_device_ids = self._grid.available_node_device_ids(await self._grid.get_status())

        # Fence: probes (asyncio.gather above) and Grid /status are slow external
        # calls. If another backend took leadership while we awaited them, drop
        # all writes from this cycle.
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
                grid_device_ids=grid_device_ids,
                observed_port=request.observed_port,
                observed_pid=request.observed_pid,
                observed_active_connection_target=request.observed_active_connection_target,
            )
            await db.commit()

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
        except (AgentUnreachableError, AgentResponseError, CircuitOpenError):
            return ProbeResult(status="indeterminate", detail="agent transport error")

    async def _attempt_node_restart(self, db: AsyncSession, *, device: Device) -> None:
        node = (await db.execute(select(AppiumNode).where(AppiumNode.device_id == device.id))).scalar_one_or_none()
        if node is None:
            return
        window_sec = int(self._settings.get("appium_reconciler.restart_window_sec"))
        deadline = datetime.now(UTC) + timedelta(seconds=window_sec)
        precondition: NodeRunningPrecondition = {
            "kind": "node_running",
            "device_id": str(device.id),
            "expected": False,
        }
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
                    precondition=precondition,
                ),
                IntentRegistration(
                    source=f"auto_recovery:recovery:{device.id}",
                    axis=RECOVERY,
                    payload={"allowed": True, "priority": PRIORITY_AUTO_RECOVERY, "reason": "Node health restart"},
                    precondition=precondition,
                ),
            ],
            reason="Max node health failures reached",
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
        grid_device_ids: set[str] | None,
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

        if result.status == "indeterminate":
            if grid_device_ids is None or str(device.id) in grid_device_ids:
                return
            if _grid_registration_grace_active(node, settings=self._settings):
                return
            # The hub answered and no longer lists this device: grid absence
            # is primary evidence of a dead node even though the agent probe
            # is inconclusive (the agent host may be down — exactly when the
            # hub's word is the only one left to go on).
            healthy = False
            logger.warning(
                "Node health check failed for device %s (port %d): relay is not registered in Selenium Grid",
                device.name,
                node.port,
            )
        else:
            healthy = result.status == "ack"

        if healthy and grid_device_ids is not None and str(device.id) not in grid_device_ids:
            if _grid_registration_grace_active(node, settings=self._settings):
                logger.info(
                    "Node health check for device %s (port %d) is waiting for Selenium Grid registration",
                    device.name,
                    node.port,
                )
                await self._health.apply_node_state_transition(
                    db,
                    device,
                    health_running=None,
                    health_state=None,
                    mark_offline=False,
                )
                return
            healthy = False
            logger.warning(
                "Node health check failed for device %s (port %d): relay is not registered in Selenium Grid",
                device.name,
                node.port,
            )

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
                    summary_state=DeviceLifecyclePolicySummaryState.idle,
                    reason="Node health checks recovered",
                    detail="The node resumed healthy operation after transient failures",
                    source="node_health",
                )
            locked_node.consecutive_health_failures = 0
            await self._health.apply_node_state_transition(
                db,
                device,
                health_running=None,
                health_state=None,
                mark_offline=False,
            )
            return

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
            return


class NodeHealthLoop:
    def __init__(self, *, services: AppiumNodeServices) -> None:
        self._services = services

    async def run(self) -> None:
        """Background loop that checks Appium node health.

        Wakes on either the doorbell (set by the hub event-bus subscriber
        on ``node-added`` / ``node-removed``) or the registry-configured
        timeout, whichever comes first. The poll continues to run as a
        drift reconciler against any bus event that was missed (hub
        restart, network partition, slow joiner).
        """
        node_health = self._services.node_health
        while True:
            interval = float(self._services.settings.get("general.node_check_interval_sec"))
            try:
                async with observe_background_loop(LOOP_NAME, interval).cycle(), self._services.session_factory() as db:
                    await node_health.check_nodes(db)
            except LeadershipLost as exc:
                logger.error(
                    "node_health_loop_leadership_lost",
                    reason=str(exc),
                    action="exiting_process_to_prevent_split_brain",
                )
                os._exit(70)
            except Exception:
                logger.exception("Node health check failed")
            woke = await node_health.wait_for_wake(interval)
            NODE_HEALTH_WAKE_SOURCE_TOTAL.labels(source="doorbell" if woke else "tick").inc()
