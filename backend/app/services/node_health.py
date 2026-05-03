import asyncio
import uuid  # noqa: TC003 — runtime use in defaultdict type annotation below
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.errors import AgentResponseError, AgentUnreachableError, CircuitOpenError
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.device_event import DeviceEventType
from app.observability import get_logger, observe_background_loop
from app.schemas.device import DeviceLifecyclePolicySummaryState
from app.services import (
    appium_resource_allocator,
    capability_service,
    control_plane_state_store,
    device_health_summary,
    grid_service,
    lifecycle_policy,
)
from app.services.agent_operations import appium_probe_session as fetch_appium_probe_session
from app.services.agent_operations import appium_status as fetch_appium_status
from app.services.device_availability import set_device_availability_status
from app.services.device_event_service import record_event
from app.services.device_readiness import is_ready_for_use_async
from app.services.event_bus import event_bus
from app.services.lifecycle_incident_service import record_lifecycle_incident
from app.services.node_manager_remote import require_management_host
from app.services.node_manager_remote import restart_node_via_agent as restart_node_via_agent_helper
from app.services.node_manager_types import NodeManagerError
from app.services.session_viability import build_probe_capabilities
from app.services.settings_service import settings_service

logger = get_logger(__name__)
NODE_HEALTH_NAMESPACE = "node_health.failure_count"
LOOP_NAME = "node_health"
NODE_HEALTH_PROBE_TIMEOUT_SEC = 15
PROBE_CONCURRENCY_PER_HOST = 2


async def _bounded_check_node_health(
    semaphore: asyncio.Semaphore,
    node: AppiumNode,
    device: Device,
    *,
    probe_capabilities: dict[str, Any] | None,
) -> bool | None:
    async with semaphore:
        return await _check_node_health(node, device, probe_capabilities=probe_capabilities)


@dataclass(frozen=True)
class NodeHealthCheckRequest:
    node: AppiumNode
    device: Device
    observed_state: NodeState
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
    return device.availability_status == DeviceAvailabilityStatus.available and await is_ready_for_use_async(db, device)


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
) -> bool | None:
    """Probe Appium node health.

    Returns True/False when the agent answered with a definitive result, None
    when reachability or the agent's HTTP layer prevented one (transport error,
    HTTP error response, or open circuit). Indeterminate results must not flip
    the snapshot or increment the failure counter — see ``_process_node_health``.
    """
    try:
        host = require_management_host(device, action="monitor Appium node health")
    except NodeManagerError:
        return False

    try:
        if probe_capabilities is not None:
            healthy, error = await fetch_appium_probe_session(
                host.ip,
                host.agent_port,
                node.port,
                capabilities=probe_capabilities,
                timeout_sec=NODE_HEALTH_PROBE_TIMEOUT_SEC,
                http_client_factory=httpx.AsyncClient,
            )
            # ``appium_probe_session`` swallows non-2xx responses into ``(False, err)``.
            # A definitive negative carries an Appium-side reason ("Probe session
            # returned an invalid payload" / explicit error from agent body); a
            # transport-shaped HTTP failure surfaces as the synthetic message
            # "Probe session failed (HTTP <code>)". Map the HTTP-shaped error to
            # indeterminate so a single transient agent 5xx does not cascade into
            # a recovery action.
            if not healthy and isinstance(error, str) and error.startswith("Probe session failed (HTTP "):
                return None
            return healthy
        payload = await fetch_appium_status(
            host.ip,
            host.agent_port,
            node.port,
            http_client_factory=httpx.AsyncClient,
        )
        # ``appium_status`` returns ``None`` for non-2xx responses. Treat that as
        # indeterminate rather than "not running" so transient HTTP errors do
        # not flip ``node_running`` to False or trigger a restart.
        if payload is None:
            return None
        return bool(payload.get("running", False))
    except (AgentUnreachableError, AgentResponseError, CircuitOpenError):
        return None


def _grid_registration_grace_active(node: AppiumNode) -> bool:
    started_at = node.started_at
    if started_at is None:
        return False
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    age_seconds = (datetime.now(UTC) - started_at).total_seconds()
    return 0 <= age_seconds < int(settings_service.get("appium.startup_timeout_sec"))


async def _restart_node_via_agent(db: AsyncSession, device: Device, node: AppiumNode) -> bool:
    """Attempt to restart a node through its host agent."""
    try:
        return await restart_node_via_agent_helper(db, device, node, http_client_factory=httpx.AsyncClient)
    except NodeManagerError:
        return False


async def reset_node_health_control_plane_state(db: AsyncSession) -> None:
    await control_plane_state_store.delete_namespace(db, NODE_HEALTH_NAMESPACE)
    await db.commit()


async def get_node_health_control_plane_state(db: AsyncSession) -> dict[str, int]:
    values = await control_plane_state_store.get_values(db, NODE_HEALTH_NAMESPACE)
    return {key: int(value) for key, value in values.items()}


async def set_node_health_failure_count(db: AsyncSession, node_key: str, count: int) -> None:
    await control_plane_state_store.set_value(db, NODE_HEALTH_NAMESPACE, node_key, count)
    await db.commit()


async def _process_node_health(
    db: AsyncSession,
    node: AppiumNode,
    device: Device,
    *,
    healthy: bool | None,
    grid_device_ids: set[str] | None,
    observed_state: NodeState | None = None,
    observed_port: int | None = None,
    observed_pid: int | None = None,
    observed_active_connection_target: str | None = None,
) -> None:
    node_key = str(node.id)

    from app.services import appium_node_locking

    locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
    if locked_node is None:
        # Node was deleted between the caller's lock_device and here. Bail out
        # quietly; lower layers will reconcile on the next sweep.
        return

    if (
        observed_state is not None
        and observed_port is not None
        and (
            locked_node.state != observed_state
            or locked_node.port != observed_port
            or locked_node.pid != observed_pid
            or locked_node.active_connection_target != observed_active_connection_target
        )
    ):
        logger.info(
            "Node health check for device %s skipped stale probe result after node changed",
            device.name,
        )
        return

    node = locked_node

    if locked_node.state != NodeState.running:
        return

    if healthy is None:
        return

    if healthy and grid_device_ids is not None and str(device.id) not in grid_device_ids:
        if _grid_registration_grace_active(node):
            logger.info(
                "Node health check for device %s (port %d) is waiting for Selenium Grid registration",
                device.name,
                node.port,
            )
            await device_health_summary.update_node_state(db, device, running=True, state=node.state.value)
            return
        healthy = False
        logger.warning(
            "Node health check failed for device %s (port %d): relay is not registered in Selenium Grid",
            device.name,
            node.port,
        )

    if healthy:
        if await control_plane_state_store.get_value(db, NODE_HEALTH_NAMESPACE, node_key) is not None:
            logger.info("Node for device %s (%s) recovered", device.name, device.identity_value)
            await lifecycle_policy.record_control_action(
                db,
                device,
                action="node_monitor_recovered",
                failure_source="node_health",
                failure_reason="Node health checks recovered",
            )
            await lifecycle_policy.clear_pending_auto_stop_on_recovery(
                db,
                device,
                source="node_health",
                reason="Node health checks recovered",
            )
            await event_bus.publish(
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
        await control_plane_state_store.delete_value(db, NODE_HEALTH_NAMESPACE, node_key)
        await device_health_summary.update_node_state(db, device, running=True, state=node.state.value)
        return

    count = await control_plane_state_store.increment_counter(db, NODE_HEALTH_NAMESPACE, node_key)
    max_failures = settings_service.get("general.node_max_failures")
    await device_health_summary.update_node_state(
        db,
        device,
        running=False,
        state="error",
        mark_offline_on_failure=count >= max_failures,
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
        await control_plane_state_store.delete_value(db, NODE_HEALTH_NAMESPACE, node_key)

        if not device.auto_manage:
            logger.info(
                "Node for device %s reached max failures but auto_manage is off — marking error without restart",
                device.name,
            )
            await appium_resource_allocator.release_owner(
                db,
                appium_resource_allocator.managed_owner_key(device.id),
            )
            await lifecycle_policy.record_control_action(
                db,
                device,
                action="recovery_suppressed",
                failure_source="node_health",
                failure_reason="Max node health failures reached",
                recovery_suppressed_reason="Auto-manage is disabled",
            )
            await event_bus.publish(
                "node.crash",
                {
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "error": "Max health check failures",
                    "will_restart": False,
                },
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
            node.state = NodeState.error
            await device_health_summary.update_node_state(db, device, running=False, state="error")
            await set_device_availability_status(device, DeviceAvailabilityStatus.offline, publish_event=False)
            return

        logger.error("Node for device %s reached max failures, attempting restart", device.name)

        restarted = await _restart_node_via_agent(db, device, node)
        if restarted:
            await lifecycle_policy.record_control_action(
                db,
                device,
                action="auto_recovered",
                failure_source="node_health",
                failure_reason="Node restarted after health failures",
            )
            await event_bus.publish(
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
                {"recovered_from": "auto_restart", "port": node.port},
            )
            await record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovered,
                summary_state=DeviceLifecyclePolicySummaryState.idle,
                reason="Node restarted after health failures",
                detail="Automatic node restart succeeded after repeated health check failures",
                source="node_health",
            )
        else:
            logger.error("Restart failed for device %s — marking offline", device.name)
            await appium_resource_allocator.release_owner(
                db,
                appium_resource_allocator.managed_owner_key(device.id),
            )
            await lifecycle_policy.record_control_action(
                db,
                device,
                action="recovery_failed",
                failure_source="node_health",
                failure_reason="Node restart failed",
                recovery_suppressed_reason="Node restart failed",
            )
            await event_bus.publish(
                "node.crash",
                {
                    "device_id": str(device.id),
                    "device_name": device.name,
                    "error": "Restart failed",
                    "will_restart": False,
                },
            )
            await record_event(
                db,
                device.id,
                DeviceEventType.node_crash,
                {"error": "Restart failed", "will_restart": False},
            )
            await record_lifecycle_incident(
                db,
                device,
                DeviceEventType.lifecycle_recovery_failed,
                summary_state=DeviceLifecyclePolicySummaryState.suppressed,
                reason="Node restart failed",
                detail="Automatic node restart failed after repeated health check failures",
                source="node_health",
            )
            node.state = NodeState.error
            await device_health_summary.update_node_state(db, device, running=False, state="error")
            await set_device_availability_status(device, DeviceAvailabilityStatus.offline, publish_event=False)


async def _check_nodes(db: AsyncSession) -> None:
    from app.services import device_locking

    stmt = (
        select(AppiumNode)
        .where(AppiumNode.state == NodeState.running)
        .options(
            selectinload(AppiumNode.device).selectinload(Device.host),
            selectinload(AppiumNode.device).selectinload(Device.appium_node),
        )
        .order_by(AppiumNode.device_id)
    )
    result = await db.execute(stmt)
    nodes = result.scalars().all()

    requests = [
        NodeHealthCheckRequest(
            node=node,
            device=node.device,
            observed_state=node.state,
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
    health_results = await asyncio.gather(
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

    for request, healthy in zip(requests, health_results, strict=True):
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
            healthy=healthy,
            grid_device_ids=grid_device_ids,
            observed_state=request.observed_state,
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
        except Exception:
            logger.exception("Node health check failed")
        await asyncio.sleep(interval)
