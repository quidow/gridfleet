import abc
import asyncio
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import Device
from app.services import appium_resource_allocator
from app.services.device_readiness import is_ready_for_use_async, readiness_error_detail_async
from app.services.node_manager_remote import (
    _build_device_owner_key,
    start_remote_temporary_node,
    stop_remote_temporary_node,
)
from app.services.node_manager_remote import (
    agent_url as _agent_url_for_device,
)
from app.services.node_manager_state import allocate_port as allocate_port_impl
from app.services.node_manager_state import candidate_ports as candidate_ports_impl
from app.services.node_manager_state import mark_node_started, mark_node_stopped
from app.services.node_service_types import NodeManagerError, NodePortConflictError, TemporaryNodeHandle
from app.services.pack_platform_resolver import resolve_pack_platform

logger = logging.getLogger(__name__)

RESTART_BACKOFF_BASE = 2
RESTART_MAX_RETRIES = 3
__all__ = [
    "NodeManager",
    "NodeManagerError",
    "RemoteNodeManager",
    "TemporaryNodeHandle",
    "allocate_port",
    "get_node_manager",
]


async def allocate_port(db: AsyncSession) -> int:
    return await allocate_port_impl(db)


async def candidate_ports(
    db: AsyncSession,
    *,
    preferred_port: int | None = None,
    exclude_ports: set[int] | None = None,
) -> list[int]:
    return await candidate_ports_impl(db, preferred_port=preferred_port, exclude_ports=exclude_ports)


class NodeManager(abc.ABC):
    @abc.abstractmethod
    async def start_node(self, db: AsyncSession, device: Device) -> AppiumNode: ...

    @abc.abstractmethod
    async def stop_node(self, db: AsyncSession, device: Device) -> AppiumNode: ...

    @abc.abstractmethod
    async def start_temporary_node(
        self,
        db: AsyncSession,
        device: Device,
        *,
        owner_key: str | None = None,
        port: int | None = None,
    ) -> TemporaryNodeHandle: ...

    @abc.abstractmethod
    async def stop_temporary_node(
        self,
        db: AsyncSession,
        device: Device,
        handle: TemporaryNodeHandle,
        *,
        release_allocations: bool = True,
    ) -> bool:
        raise NotImplementedError

    async def restart_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        """Stop then start with exponential backoff on failure."""
        if device.appium_node and device.appium_node.state == NodeState.running:
            await self.stop_node(db, device)

        last_error = None
        for attempt in range(RESTART_MAX_RETRIES):
            try:
                return await self.start_node(db, device)
            except NodeManagerError as e:
                last_error = e
                wait = RESTART_BACKOFF_BASE**attempt
                logger.warning(
                    "Restart attempt %d failed for device %s, retrying in %ds: %s", attempt + 1, device.id, wait, e
                )
                await asyncio.sleep(wait)

        raise NodeManagerError(f"Failed to restart node after {RESTART_MAX_RETRIES} attempts: {last_error}")


class RemoteNodeManager(NodeManager):
    """Delegates to a host agent over HTTP."""

    async def _agent_url(self, device: Device) -> str:
        return await _agent_url_for_device(device)

    async def _start_with_owner(
        self,
        db: AsyncSession,
        device: Device,
        *,
        owner_key: str,
        preferred_port: int | None = None,
        release_allocations_on_failure: bool = True,
    ) -> TemporaryNodeHandle:
        if device.host_id is None:
            raise NodeManagerError(f"Device {device.id} has no host assigned — cannot start Appium nodes")
        resource_ports: dict[str, int] = {}
        needs_derived_data_path = False
        try:
            resolved = await resolve_pack_platform(
                db,
                pack_id=device.pack_id,
                platform_id=device.platform_id,
                device_type=device.device_type.value if device.device_type else None,
            )
            resource_ports = {p.capability_name: p.start for p in resolved.parallel_resources.ports}
            needs_derived_data_path = resolved.parallel_resources.derived_data_path
        except LookupError:
            pass
        allocated_caps = await appium_resource_allocator.get_or_create_owner_capabilities(
            db,
            owner_key=owner_key,
            host_id=device.host_id,
            resource_ports=resource_ports,
            needs_derived_data_path=needs_derived_data_path,
        )
        agent_base = await self._agent_url(device)
        try:
            last_conflict: NodePortConflictError | None = None
            for port in await candidate_ports(db, preferred_port=preferred_port):
                try:
                    handle = await start_remote_temporary_node(
                        db,
                        device,
                        port=port,
                        allocated_caps=allocated_caps,
                        agent_base=agent_base,
                        http_client_factory=httpx.AsyncClient,
                    )
                    break
                except NodePortConflictError as exc:
                    last_conflict = exc
                    logger.warning(
                        "Managed Appium port conflict for device %s on port %d; trying next candidate",
                        device.id,
                        port,
                    )
            else:
                assert last_conflict is not None
                raise last_conflict
        except Exception:
            if release_allocations_on_failure:
                await appium_resource_allocator.release_owner(db, owner_key)
                await db.commit()
            raise
        handle.owner_key = owner_key
        handle.allocated_caps = allocated_caps
        return handle

    async def start_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        if device.appium_node and device.appium_node.state == NodeState.running:
            raise NodeManagerError(f"Node already running for device {device.id}")
        if not await is_ready_for_use_async(db, device):
            raise NodeManagerError(await readiness_error_detail_async(db, device, action="start a node"))

        owner_key = _build_device_owner_key(device)
        handle = await self.start_temporary_node(db, device, owner_key=owner_key)
        return await mark_node_started(
            db,
            device,
            port=handle.port,
            pid=handle.pid,
            active_connection_target=handle.active_connection_target,
        )

    async def stop_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        node = device.appium_node
        if not node or node.state != NodeState.running:
            raise NodeManagerError(f"No running node for device {device.id}")

        handle = TemporaryNodeHandle(
            port=node.port,
            pid=node.pid,
            active_connection_target=node.active_connection_target,
            agent_base=await self._agent_url(device),
            owner_key=appium_resource_allocator.managed_owner_key(device.id),
        )
        # If the agent doesn't acknowledge the stop, refuse to mark the node
        # stopped in the DB — otherwise the orphaned Appium process keeps
        # serving traffic via Selenium Grid while the manager believes it is
        # gone (and the next start attempt fails with port collision).
        if not await self.stop_temporary_node(db, device, handle):
            raise NodeManagerError(
                f"Agent did not acknowledge stop for device {device.id} on port {node.port}; "
                "leaving node state unchanged"
            )
        return await mark_node_stopped(db, device)

    async def start_temporary_node(
        self,
        db: AsyncSession,
        device: Device,
        *,
        owner_key: str | None = None,
        port: int | None = None,
    ) -> TemporaryNodeHandle:
        resolved_owner_key = owner_key or _build_device_owner_key(device)
        if (
            device.id is not None
            and device.appium_node is not None
            and device.appium_node.state == NodeState.running
            and resolved_owner_key == appium_resource_allocator.managed_owner_key(device.id)
        ):
            return TemporaryNodeHandle(
                port=device.appium_node.port,
                pid=device.appium_node.pid,
                active_connection_target=device.appium_node.active_connection_target,
                reused_existing=True,
                agent_base=await self._agent_url(device),
                owner_key=resolved_owner_key,
                allocated_caps=await appium_resource_allocator.get_owner_capabilities(db, resolved_owner_key),
            )
        return await self._start_with_owner(
            db,
            device,
            owner_key=resolved_owner_key,
            preferred_port=port,
        )

    async def stop_temporary_node(
        self,
        db: AsyncSession,
        device: Device,
        handle: TemporaryNodeHandle,
        *,
        release_allocations: bool = True,
    ) -> bool:
        """Stop the temporary node identified by ``handle`` via its agent.

        Returns True on confirmed agent acknowledgement (or when the handle
        represents a re-used pre-existing node that the caller never owned and
        therefore should not stop). Returns False when the agent did not
        acknowledge — caller MUST NOT mutate DB node state in that case.
        """
        if handle.reused_existing:
            return True
        agent_base = handle.agent_base or await self._agent_url(device)
        stopped = await stop_remote_temporary_node(
            port=handle.port,
            agent_base=agent_base,
            http_client_factory=httpx.AsyncClient,
        )
        # Only release the owner allocation when the agent confirmed the stop.
        # An unacknowledged stop may leave the Appium process and its allocated
        # ports in use; freeing the owner here would let the allocator hand the
        # same resources to a new owner while the orphan is still running.
        if stopped and release_allocations and handle.owner_key:
            await appium_resource_allocator.release_owner(db, handle.owner_key)
            await db.commit()
        return stopped

    async def restart_node(self, db: AsyncSession, device: Device) -> AppiumNode:
        if not device.appium_node or device.appium_node.state != NodeState.running:
            return await self.start_node(db, device)

        node = device.appium_node
        owner_key = appium_resource_allocator.managed_owner_key(device.id)
        handle = TemporaryNodeHandle(
            port=node.port,
            pid=node.pid,
            active_connection_target=node.active_connection_target,
            agent_base=await self._agent_url(device),
            owner_key=owner_key,
        )
        # If the agent doesn't acknowledge the stop, do not mark the node
        # stopped and do not attempt restart on a different port — the orphan
        # process will collide.
        if not await self.stop_temporary_node(db, device, handle, release_allocations=False):
            raise NodeManagerError(
                f"Agent did not acknowledge stop during restart for device {device.id} "
                f"on port {node.port}; leaving node state unchanged"
            )
        await mark_node_stopped(db, device)

        last_error = None
        for attempt in range(RESTART_MAX_RETRIES):
            try:
                restarted = await self._start_with_owner(
                    db,
                    device,
                    owner_key=owner_key,
                    preferred_port=node.port,
                    release_allocations_on_failure=False,
                )
                return await mark_node_started(
                    db,
                    device,
                    port=restarted.port,
                    pid=restarted.pid,
                    active_connection_target=restarted.active_connection_target,
                )
            except NodeManagerError as exc:
                last_error = exc
                wait = RESTART_BACKOFF_BASE**attempt
                logger.warning(
                    "Restart attempt %d failed for device %s, retrying in %ds: %s",
                    attempt + 1,
                    device.id,
                    wait,
                    exc,
                )
                await asyncio.sleep(wait)

        await appium_resource_allocator.release_owner(db, owner_key)
        await db.commit()
        raise NodeManagerError(f"Failed to restart node after {RESTART_MAX_RETRIES} attempts: {last_error}")


# Singleton instances
_remote_manager = RemoteNodeManager()


def get_node_manager(device: Device) -> NodeManager:
    """All managed Appium operations follow the host-backed remote-manager contract."""
    _ = device
    return _remote_manager
