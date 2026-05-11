"""Operator-facing Appium node desired-state facades."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

from app.models.appium_node import AppiumNode, NodeState
from app.services.appium_reconciler_agent import (
    AVD_LAUNCH_HTTP_TIMEOUT_SECS,
    agent_url,
    appium_status,
    build_agent_start_payload,
    require_management_host,
)
from app.services.appium_reconciler_allocation import candidate_ports
from app.services.desired_state_writer import DesiredStateCaller, write_desired_state
from app.services.device_readiness import is_ready_for_use_async, readiness_error_detail_async
from app.services.node_service_common import (
    build_appium_driver_caps,
    build_grid_stereotype_caps,
    get_default_plugins,
)
from app.services.node_service_types import NodeManagerError, NodePortConflictError, TemporaryNodeHandle
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.device import Device

__all__ = [
    "AVD_LAUNCH_HTTP_TIMEOUT_SECS",
    "NodeManagerError",
    "NodePortConflictError",
    "TemporaryNodeHandle",
    "agent_url",
    "allocate_port",
    "appium_status",
    "build_agent_start_payload",
    "build_appium_driver_caps",
    "build_grid_stereotype_caps",
    "candidate_ports",
    "get_default_plugins",
    "require_management_host",
    "restart_node",
    "start_node",
    "stop_node",
]


async def allocate_port(db: AsyncSession, *, host_id: uuid.UUID) -> int:
    return (await candidate_ports(db, host_id=host_id))[0]


async def start_node(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller = "operator_route",
) -> AppiumNode:
    if device.appium_node and device.appium_node.observed_running:
        raise NodeManagerError(f"Node already running for device {device.id}")
    if not await is_ready_for_use_async(db, device):
        raise NodeManagerError(await readiness_error_detail_async(db, device, action="start a node"))

    if device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned")
    desired_port = (await candidate_ports(db, host_id=device.host_id))[0]
    if device.appium_node is None:
        node = AppiumNode(
            device_id=device.id,
            port=desired_port,
            grid_url=settings_service.get("grid.hub_url"),
        )
        db.add(node)
        await db.flush()
        device.appium_node = node

    node = cast("AppiumNode", device.appium_node)
    await write_desired_state(
        db,
        node=node,
        target=NodeState.running,
        caller=caller,
        desired_port=desired_port,
    )
    await db.commit()
    await db.refresh(node)
    return node


async def stop_node(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller = "operator_route",
) -> AppiumNode:
    node = cast("AppiumNode | None", device.appium_node)
    if not node or not node.observed_running:
        raise NodeManagerError(f"No running node for device {device.id}")

    await write_desired_state(
        db,
        node=node,
        target=NodeState.stopped,
        caller=caller,
    )
    await db.commit()
    await db.refresh(node)
    return node


async def restart_node(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller = "operator_restart",
) -> AppiumNode:
    if not device.appium_node or not device.appium_node.observed_running:
        return await start_node(db, device, caller=caller)

    node = cast("AppiumNode", device.appium_node)
    window_sec = int(settings_service.get("appium_reconciler.restart_window_sec"))
    await write_desired_state(
        db,
        node=node,
        target=NodeState.running,
        caller=caller,
        desired_port=node.port,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=window_sec),
    )
    await db.commit()
    await db.refresh(node)
    return node
