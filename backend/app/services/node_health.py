import asyncio
import os
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.models.appium_node import AppiumNode
from app.models.device import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.models.device_event import DeviceEventType
from app.observability import get_logger, observe_background_loop
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services import (
    appium_node_locking,
    capability_service,
    device_health,
    device_locking,
    grid_service,
    lifecycle_policy,
)
from app.services.agent_operations import appium_status as fetch_appium_status
from app.services.agent_probe_result import ProbeResult, from_status_response
from app.services.appium_reconciler_agent import require_management_host
from app.services.control_plane_leader import LeadershipLost, assert_current_leader
from app.services.device_event_service import record_event
from app.services.device_readiness import is_ready_for_use_async
from app.services.event_bus import queue_device_crashed_event, queue_event_for_session
from app.services.intent_service import register_intents_and_reconcile
from app.services.intent_types import NODE_PROCESS, PRIORITY_AUTO_RECOVERY, RECOVERY, IntentRegistration
from app.services.lifecycle_incident_service import record_lifecycle_incident
from app.services.node_service_types import NodeManagerError
from app.services.session_viability import (
    build_probe_capabilities,
    grid_probe_response_to_result,
    probe_session_via_grid,
)
from app.settings import settings_service

logger = get_logger(__name__)
LOOP_NAME = "node_health"
NODE_HEALTH_PROBE_TIMEOUT_SEC = 15
PROBE_CONCURRENCY_PER_HOST = 2


async def _bounded_check_node_health(
    semaphore: asyncio.Semaphore,
    node: AppiumNode,
    device: Device,
    *,
    probe_capabilities: dict[str, Any] | None,
) -> ProbeResult:
    async with semaphore:
        return await _check_node_health(node, device, probe_capabilities=probe_capabilities)


@dataclass(frozen=True)
class NodeHealthCheckRequest:
    node: AppiumNode
    device: Device
    observed_port: int
    observed_pid: int | None
    observed_active_connection_target: str | None
    probe_capabilities: dict[str, Any] | None = None


async def _should_probe_node_health(db: AsyncSession, device: Device) -> bool:
    if (
        device.pack_id == "appium-xcuitest"
        and device.platform_id in {"ios", "tvos"}
        and device.device_type == DeviceType.real_device
    ):
        return False
    if (
        device.device_type in {DeviceType.emulator, DeviceType.simulator}
        or device.connection_type == ConnectionType.virtual
    ):
        return False
    return (
        device.operational_state == DeviceOperationalState.available
        and device.hold is None
        and await is_ready_for_use_async(db, device)
    )


async def _build_probe_capabilities_for_node(db: AsyncSession, device: Device) -> dict[str, Any] | None:
    if not await _should_probe_node_health(db, device):
        return None

    try:
        capabilities = await capability_service.get_device_capabilities(db, device)
    except Exception:
        logger.exception("Failed to build node health probe capabilities for device %s", device.id)
        return None

    return build_probe_capabilities(capabilities)


async def _check_node_health(
    node: AppiumNode,
    device: Device,
    *,
    probe_capabilities: dict[str, Any] | None = None,
) -> ProbeResult:
    try:
        host = require_management_host(device, action="monitor Appium node health")
    except NodeManagerError:
        return ProbeResult(status="refused", detail="no management host")

    try:
        if probe_capabilities is not None:
            result = await probe_session_via_grid(
                probe_capabilities,
                NODE_HEALTH_PROBE_TIMEOUT_SEC,
                grid_url=node.grid_url,
            )
            return grid_probe_response_to_result(result)
        payload = await fetch_appium_status(
            host.ip,
            host.agent_port,
            node.port,
            http_client_factory=httpx.AsyncClient,
        )
        return from_status_response(payload)
    except (AgentUnreachableError, AgentResponseError, CircuitOpenError):
        return ProbeResult(status="indeterminate", detail="agent transport error")


def _grid_registration_grace_active(node: AppiumNode) -> bool:
    started_at = node.started_at
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - started_at).total_seconds()
    return 0 <= age_seconds < int(settings_service.get("appium.startup_timeout_sec"))


async def _process_node_health(
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
        return
    healthy = result.status == "ack"

    if healthy and grid_device_ids is not None and str(device.id) not in grid_device_ids:
        if _grid_registration_grace_active(node):
            logger.info(
                "Node health check for device %s (port %d) is waiting for Selenium Grid registration",
                device.name,
                node.port,
            )
            await device_health.apply_node_state_transition(
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
            await lifecycle_policy.record_control_action(
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
            await lifecycle_policy.clear_pending_auto_stop_on_recovery(
                db,
                device,
                source="node_health",
                reason="Node health checks recovered",
                record_incident=False,
            )
            queue_event_for_session(
                db,
                "node.state_changed",
                {
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "old_state": "error",
                    "new_state": "running",
                    "port": node.port,
                },
            )
            await record_event(
                db,
                device.id,
                DeviceEventType.node_restart,
                {"recovered_from": "health_check_failure", "port": node.port},
            )
            await record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovered,
                summary_state=DeviceLifecyclePolicySummaryState.idle,
                reason="Node health checks recovered",
                detail="The node resumed healthy operation after transient failures",
                source="node_health",
            )
        locked_node.consecutive_health_failures = 0
        await device_health.apply_node_state_transition(
            db,
            device,
            health_running=None,
            health_state=None,
            mark_offline=False,
        )
        return

    locked_node.consecutive_health_failures += 1
    count = locked_node.consecutive_health_failures
    max_failures = settings_service.get("general.node_max_failures")
    await device_health.apply_node_state_transition(
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

        if not device.auto_manage:
            logger.info(
                "Node for device %s reached max failures but auto_manage is off — marking error without restart",
                device.name,
            )
            # Do not release parallel-resource claims here. A health failure
            # does not prove the Appium process is gone, so freeing ports here
            # can race a still-listening process. Confirmed stop paths release;
            # node deletion cascades managed claims.
            await lifecycle_policy.record_control_action(
                db,
                device,
                action="recovery_suppressed",
                failure_source="node_health",
                failure_reason="Max node health failures reached",
                recovery_suppressed_reason="Auto-manage is disabled",
            )
            queue_event_for_session(
                db,
                "node.crash",
                {
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "error": "Max health check failures",
                    "will_restart": False,
                },
            )
            queue_device_crashed_event(
                db,
                device_id=str(device.id),
                device_name=device.name,
                source="health_check_fail",
                reason="Max health check failures",
                will_restart=False,
                process=None,
            )
            await record_event(
                db,
                device.id,
                DeviceEventType.node_crash,
                {"error": "Max health check failures", "will_restart": False},
            )
            await record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovery_suppressed,
                summary_state=DeviceLifecyclePolicySummaryState.suppressed,
                reason="Auto-manage is disabled",
                detail="Node restart was suppressed after repeated health check failures",
                source="node_health",
            )
            await device_health.apply_node_state_transition(
                db,
                device,
                health_running=False,
                health_state="error",
                mark_offline=True,
            )
            return

        logger.error("Node for device %s reached max failures, attempting restart", device.name)

        window_sec = int(settings_service.get("appium_reconciler.restart_window_sec"))
        deadline = datetime.now(UTC) + timedelta(seconds=window_sec)
        await register_intents_and_reconcile(
            db,
            device_id=device.id,
            intents=[
                IntentRegistration(
                    source=f"auto_recovery:node:{device.id}",
                    axis=NODE_PROCESS,
                    payload={
                        "action": "start",
                        "priority": PRIORITY_AUTO_RECOVERY,
                        "desired_port": node.port,
                        "transition_token": str(uuid.uuid4()),
                        "transition_deadline": deadline.isoformat(),
                    },
                ),
                IntentRegistration(
                    source=f"auto_recovery:recovery:{device.id}",
                    axis=RECOVERY,
                    payload={"allowed": True, "priority": PRIORITY_AUTO_RECOVERY, "reason": "Node health restart"},
                ),
            ],
            reason="Max node health failures reached",
        )
        await db.commit()
        return


async def _check_nodes(db: AsyncSession) -> None:
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
            probe_capabilities=await _build_probe_capabilities_for_node(db, node.device),
        )
        for node in nodes
    ]
    host_semaphores: defaultdict[uuid.UUID, asyncio.Semaphore] = defaultdict(
        lambda: asyncio.Semaphore(PROBE_CONCURRENCY_PER_HOST)
    )
    results = await asyncio.gather(
        *[
            _bounded_check_node_health(
                host_semaphores[request.device.host_id],
                request.node,
                request.device,
                probe_capabilities=request.probe_capabilities,
            )
            for request in requests
        ]
    )
    grid_device_ids = grid_service.available_node_device_ids(await grid_service.get_grid_status())

    # Fence: probes (asyncio.gather above) and Grid /status are slow external
    # calls. If another backend took leadership while we awaited them, drop
    # all writes from this cycle.
    await assert_current_leader(db)

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

        await _process_node_health(
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


async def node_health_loop() -> None:
    """Background loop that checks Appium node health."""
    while True:
        interval = float(settings_service.get("general.node_check_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _check_nodes(db)
        except LeadershipLost as exc:
            logger.error(
                "node_health_loop_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            logger.exception("Node health check failed")
        await asyncio.sleep(interval)
