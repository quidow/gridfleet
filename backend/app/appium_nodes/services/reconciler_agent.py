"""Single-module node lifecycle service."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import httpx
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.appium_nodes.exceptions import NodeManagerError, NodePortConflictError, RemoteStartResult
from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services import (
    capability_keys as appium_capability_keys,
)
from app.appium_nodes.services import (
    locking as appium_node_locking,
)
from app.appium_nodes.services import (
    resource_service as appium_node_resource_service,
)
from app.appium_nodes.services.common import (
    build_appium_driver_caps,
    build_grid_stereotype_caps,
    get_default_plugins,
)
from app.appium_nodes.services.desired_state_writer import DesiredStateCaller, write_desired_state
from app.appium_nodes.services.reconciler_allocation import (
    APPIUM_PORT_CAPABILITY,
    candidate_ports,
    reserve_appium_port,
)
from app.database import async_session
from app.errors import AgentCallError
from app.events import queue_event_for_session
from app.models.device import Device, DeviceOperationalState
from app.packs.services import capability as pack_capability
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver
from app.packs.services import start_shim as pack_start_shim
from app.services import (
    agent_operations,
    device_health,
    device_locking,
)
from app.services.agent_error_codes import AgentErrorCode
from app.services.agent_operations import (
    appium_start,
    appium_stop,
    parse_agent_error_detail,
    response_json_dict,
)
from app.services.device_identity import appium_connection_target
from app.services.device_readiness import is_ready_for_use_async, readiness_error_detail_async
from app.services.device_state import ready_operational_state, set_operational_state
from app.services.lifecycle_policy_actions import reset_reconciler_start_failure_state
from app.services.lifecycle_policy_state import (
    record_manual_recovered,
)
from app.services.lifecycle_policy_state import (
    state as lifecycle_policy_state,
)
from app.services.lifecycle_policy_state import (
    write_state as write_lifecycle_policy_state,
)
from app.settings import settings_service

assert_runnable = pack_platform_resolver.assert_runnable
build_pack_start_payload = pack_start_shim.build_pack_start_payload
device_is_virtual = pack_platform_catalog.device_is_virtual
render_default_capabilities = pack_capability.render_default_capabilities
render_device_field_capabilities = pack_capability.render_device_field_capabilities
render_stereotype = pack_capability.render_stereotype
resolve_pack_for_device = pack_start_shim.resolve_pack_for_device
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform
PackStartPayloadError = pack_start_shim.PackStartPayloadError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_client import AgentClientFactory
    from app.models.host import Host

logger = logging.getLogger(__name__)

RESTART_BACKOFF_BASE = 2
RESTART_MAX_RETRIES = 3
AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190

appium_status = agent_operations.appium_status


def _short_session_factory(db: AsyncSession) -> async_sessionmaker[AsyncSession]:
    if db.bind is None:
        return async_session
    return async_sessionmaker(db.bind, expire_on_commit=False)


__all__ = [
    "AVD_LAUNCH_HTTP_TIMEOUT_SECS",
    "RESTART_BACKOFF_BASE",
    "RESTART_MAX_RETRIES",
    "NodeManagerError",
    "NodePortConflictError",
    "RemoteStartResult",
    "agent_url",
    "allocate_port",
    "build_agent_start_payload",
    "candidate_ports",
    "mark_node_started",
    "mark_node_stopped",
    "require_management_host",
    "restart_node",
    "restart_node_via_agent",
    "start_node",
    "start_remote_node",
    "stop_node",
    "stop_node_via_agent",
    "stop_remote_node",
    "wait_for_node_running",
]


async def _hold_device_row_lock(db: AsyncSession, device_id: uuid.UUID) -> Device:
    """Acquire and refresh the Device row used for node-state writes."""
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound:
        raise NodeManagerError(f"Device {device_id} no longer exists") from None


async def allocate_port(db: AsyncSession, *, host_id: uuid.UUID) -> int:
    return (await candidate_ports(db, host_id=host_id))[0]


def upsert_node(
    db: AsyncSession,
    device: Device,
    port: int,
    pid: int | None,
    active_connection_target: str | None,
) -> AppiumNode:
    if device.appium_node:
        node = cast("AppiumNode", device.appium_node)
        node.port = port
        node.grid_url = settings_service.get("grid.hub_url")
        node.pid = pid
        node.active_connection_target = active_connection_target
    else:
        node = AppiumNode(
            device_id=device.id,
            port=port,
            grid_url=settings_service.get("grid.hub_url"),
            pid=pid,
            active_connection_target=active_connection_target,
        )
        db.add(node)
    device.appium_node = node
    return node


async def mark_node_started(
    db: AsyncSession,
    device: Device,
    *,
    port: int,
    pid: int | None,
    active_connection_target: str | None = None,
    allocated_caps: dict[str, Any] | None = None,
    clear_transition: bool = False,
) -> AppiumNode:
    device = await _hold_device_row_lock(db, device.id)
    await appium_node_locking.lock_appium_node_for_device(db, device.id)
    active_connection_target = active_connection_target or appium_connection_target(device)
    node = upsert_node(db, device, port, pid, active_connection_target)
    await db.flush()
    if device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned — cannot promote Appium resource claims")
    for key, value in (allocated_caps or {}).items():
        if isinstance(value, int):
            continue
        await appium_node_resource_service.set_node_extra_capability(
            db,
            node_id=node.id,
            capability_key=key,
            value=value,
        )
    await set_operational_state(device, await ready_operational_state(db, device), reason="Node started")
    await device_health.apply_node_state_transition(
        db,
        device,
        mark_offline=False,
    )
    reset_reconciler_start_failure_state(device)
    if clear_transition:
        node.transition_token = None
        node.transition_deadline = None
    queue_event_for_session(
        db,
        "node.state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_state": "stopped",
            "new_state": "running",
            "port": port,
        },
    )
    await db.commit()
    await db.refresh(node)
    return node


async def mark_node_stopped(db: AsyncSession, device: Device) -> AppiumNode:
    device = await _hold_device_row_lock(db, device.id)
    node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
    assert node is not None
    node.pid = None
    node.active_connection_target = None
    await set_operational_state(device, DeviceOperationalState.offline, reason="Node stopped")
    await device_health.apply_node_state_transition(
        db,
        device,
        mark_offline=False,
    )
    queue_event_for_session(
        db,
        "node.state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_state": "running",
            "new_state": "stopped",
        },
    )
    await db.commit()
    await db.refresh(node)
    return node


def require_management_host(device: Device, *, action: str = "use remote management") -> Host:
    host = cast("Host | None", device.host)
    if host is None or device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned — cannot {action}")
    return host


async def agent_url(device: Device) -> str:
    host = require_management_host(device)
    return f"http://{host.ip}:{host.agent_port}"


def build_agent_start_payload(
    device: Device,
    port: int,
    *,
    allocated_caps: dict[str, Any] | None = None,
    extra_caps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headless = (device.tags or {}).get("emulator_headless", "true") != "false"
    manager_owned_keys = appium_capability_keys.manager_owned_cap_keys(frozenset((allocated_caps or {}).keys()))
    node = (
        None if "appium_node" in sqlalchemy_inspect(device).unloaded else cast("AppiumNode | None", device.appium_node)
    )
    accepting_new_sessions = node.accepting_new_sessions if node is not None else True
    stop_pending = node.stop_pending if node is not None else False
    grid_run_id = node.desired_grid_run_id if node is not None else None
    stereotype_caps = (
        build_grid_stereotype_caps(
            device,
            session_caps=allocated_caps,
            extra_caps=extra_caps,
            manager_owned_keys=manager_owned_keys,
        )
        or {}
    )
    stereotype_caps["gridfleet:run_id"] = str(grid_run_id) if grid_run_id else "free"
    stereotype_caps["gridfleet:available"] = accepting_new_sessions
    return {
        "connection_target": appium_connection_target(device),
        "platform_id": device.platform_id,
        "port": port,
        "grid_url": settings_service.get("grid.hub_url"),
        "plugins": get_default_plugins() or None,
        "extra_caps": extra_caps
        if extra_caps is not None
        else (
            build_appium_driver_caps(
                device,
                session_caps=allocated_caps,
                manager_owned_keys=manager_owned_keys,
            )
            or None
        ),
        "stereotype_caps": stereotype_caps or None,
        "accepting_new_sessions": accepting_new_sessions,
        "stop_pending": stop_pending,
        "grid_run_id": str(grid_run_id) if grid_run_id else None,
        "device_type": device.device_type.value,
        "ip_address": device.ip_address,
        "allocated_caps": allocated_caps or None,
        "session_override": settings_service.get("appium.session_override"),
        "headless": headless,
    }


async def _build_appium_default_pack_caps(db: AsyncSession, device: Device) -> dict[str, Any]:
    resolved = resolve_pack_for_device(device)
    if resolved is None:
        return {}
    pack_id, platform_id = resolved
    resolved_plat = await resolve_pack_platform(
        db,
        pack_id=pack_id,
        platform_id=platform_id,
        device_type=device.device_type.value if device.device_type else None,
    )
    device_context = {
        "ip_address": device.ip_address,
        "connection_target": getattr(device, "connection_target", None),
        "identity_value": getattr(device, "identity_value", None),
        "os_version": device.os_version,
    }
    caps = render_default_capabilities(resolved_plat, device_context=device_context)
    caps.update(render_device_field_capabilities(resolved_plat, device.device_config or {}))
    return caps


async def _merge_appium_default_pack_caps(db: AsyncSession, device: Device, payload: dict[str, Any]) -> None:
    pack_caps = await _build_appium_default_pack_caps(db, device)
    if not pack_caps:
        return
    payload["extra_caps"] = {
        **(payload.get("extra_caps") or {}),
        **pack_caps,
    }


def _agent_start_timeout(device: Device) -> float | int:
    base = int(settings_service.get("appium.startup_timeout_sec")) + 5
    if device_is_virtual(device):
        return max(AVD_LAUNCH_HTTP_TIMEOUT_SECS, base)
    return base


async def start_remote_node(
    db: AsyncSession,
    device: Device,
    *,
    port: int,
    allocated_caps: dict[str, Any] | None,
    agent_base: str,
    http_client_factory: AgentClientFactory,
) -> RemoteStartResult:
    await assert_runnable(db, pack_id=device.pack_id, platform_id=device.platform_id)
    host = require_management_host(device, action="start Appium nodes")

    # Resolve stereotype once for both consumers (_build_session_aligned_start_caps
    # and build_pack_start_payload) to avoid duplicate DB queries.
    resolved_pack = resolve_pack_for_device(device)
    if resolved_pack is None:
        raise NodeManagerError(f"Device {device.id} has no driver pack platform")
    stereotype = await render_stereotype(db, pack_id=resolved_pack[0], platform_id=resolved_pack[1])
    plat = await resolve_pack_platform(db, pack_id=resolved_pack[0], platform_id=resolved_pack[1])
    appium_platform_name = plat.appium_platform_name
    extra_caps = await _build_session_aligned_start_caps(
        db,
        device,
        allocated_caps=allocated_caps,
        stereotype=stereotype,
        appium_platform_name=appium_platform_name,
    )
    payload = build_agent_start_payload(
        device,
        port,
        allocated_caps=allocated_caps,
        extra_caps=extra_caps,
    )
    await _merge_appium_default_pack_caps(db, device, payload)
    try:
        pack_overrides = await build_pack_start_payload(db, device=device, stereotype=stereotype)
    except PackStartPayloadError as exc:
        raise NodeManagerError(str(exc)) from exc
    if pack_overrides is not None:
        payload["pack_id"] = pack_overrides["pack_id"]
        payload["platform_id"] = pack_overrides["platform_id"]
        payload["appium_platform_name"] = pack_overrides["appium_platform_name"]
        payload["stereotype_caps"] = {
            **(payload.get("stereotype_caps") or {}),
            **pack_overrides["stereotype_caps"],
        }
        if "grid_slots" in pack_overrides:
            payload["grid_slots"] = pack_overrides["grid_slots"]
        for key in ("lifecycle_actions", "connection_behavior", "insecure_features", "workaround_env"):
            if key in pack_overrides:
                payload[key] = pack_overrides[key]
    try:
        resp = await appium_start(
            agent_base,
            host=host.ip,
            agent_port=host.agent_port,
            payload=payload,
            http_client_factory=http_client_factory,
            timeout=_agent_start_timeout(device),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code, message_text = parse_agent_error_detail(exc.response)
        message = f"Agent failed to start node: {message_text}"
        if code in {AgentErrorCode.PORT_OCCUPIED.value, AgentErrorCode.ALREADY_RUNNING.value}:
            raise NodePortConflictError(message) from exc
        raise NodeManagerError(message) from exc
    except AgentCallError:
        raise
    except httpx.HTTPError as exc:
        raise NodeManagerError(f"Cannot reach agent at {agent_base}: {exc}") from exc

    data = response_json_dict(resp)
    active_connection_target = data.get("connection_target")
    return RemoteStartResult(
        port=port,
        pid=data.get("pid"),
        active_connection_target=active_connection_target if isinstance(active_connection_target, str) else None,
        agent_base=agent_base,
    )


async def _build_session_aligned_start_caps(
    db: AsyncSession,
    device: Device,
    *,
    allocated_caps: dict[str, Any] | None,
    stereotype: dict[str, Any] | None = None,
    appium_platform_name: str | None = None,
) -> dict[str, Any]:
    """Build extra_caps aligned with what a session probe would request.

    Avoids accessing lazily-loaded relationships (e.g. device.appium_node).
    If *stereotype* is provided (pre-resolved by the caller) it is used directly
    and no additional DB query is issued.
    """
    manager_owned_keys = appium_capability_keys.manager_owned_cap_keys(frozenset((allocated_caps or {}).keys()))
    caps: dict[str, Any] = build_appium_driver_caps(
        device,
        session_caps=allocated_caps,
        manager_owned_keys=manager_owned_keys,
    )
    if stereotype is None:
        resolved = resolve_pack_for_device(device)
        if resolved is not None:
            pack_id, platform_id = resolved
            stereotype = await render_stereotype(db, pack_id=pack_id, platform_id=platform_id)
    if stereotype is not None:
        automation_name = stereotype.get("appium:automationName")
        if automation_name:
            caps["appium:automationName"] = automation_name
    return caps


async def stop_remote_node(
    *,
    port: int,
    agent_base: str,
    host: str,
    agent_port: int,
    http_client_factory: AgentClientFactory,
) -> bool:
    """Ask the agent to stop the Appium node on ``port``.

    Returns True on confirmed agent acknowledgement, False otherwise. Callers
    that mutate DB node state on the back of a stop MUST gate that mutation on
    True — a False return means we cannot prove the agent process is gone, and
    flipping the DB to ``stopped`` would leave the agent process orphaned with
    its Selenium Grid registration intact.
    """
    try:
        resp = await appium_stop(
            agent_base,
            host=host,
            agent_port=agent_port,
            port=port,
            http_client_factory=http_client_factory,
        )
        resp.raise_for_status()
        return True
    except (AgentCallError, httpx.HTTPError):
        return False


async def stop_node_via_agent(
    device: Device,
    node: AppiumNode,
    *,
    http_client_factory: AgentClientFactory,
) -> bool:
    try:
        host = require_management_host(device, action="stop Appium nodes")
    except NodeManagerError:
        return False
    try:
        resp = await appium_stop(
            f"http://{host.ip}:{host.agent_port}",
            host=host.ip,
            agent_port=host.agent_port,
            port=node.port,
            http_client_factory=http_client_factory,
        )
        resp.raise_for_status()
        return True
    except (AgentCallError, httpx.HTTPError):
        return False


async def restart_node_via_agent(
    db: AsyncSession,
    device: Device,
    node: AppiumNode,
    *,
    http_client_factory: AgentClientFactory,
) -> bool:
    try:
        host = require_management_host(device, action="restart Appium nodes")
    except NodeManagerError:
        return False

    device = await device_locking.lock_device(db, device.id)
    locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
    if locked_node is None:
        return False
    node = locked_node

    agent_base = f"http://{host.ip}:{host.agent_port}"
    allocated_caps = await appium_node_resource_service.get_capabilities(db, node_id=node.id) or None

    try:
        stopped = await stop_remote_node(
            port=node.port,
            agent_base=agent_base,
            host=host.ip,
            agent_port=host.agent_port,
            http_client_factory=http_client_factory,
        )
        if not stopped:
            # Agent did not acknowledge the stop. Starting on a different
            # candidate port now would race the orphan Appium/Grid relay that
            # may still be alive on the old port. Refuse to proceed and let the
            # caller retry once the agent is reachable again.
            return False

        last_conflict: NodePortConflictError | None = None
        started_handle: RemoteStartResult | None = None
        # The DB row still says the old node is running, so candidate_ports()
        # intentionally excludes node.port here. That is desirable after an
        # unmanaged-listener conflict: restart on the next free managed port.
        for candidate_port in await candidate_ports(db, host_id=device.host_id, preferred_port=node.port):
            try:
                started_handle = await start_remote_node(
                    db,
                    device,
                    port=candidate_port,
                    allocated_caps=allocated_caps,
                    agent_base=agent_base,
                    http_client_factory=http_client_factory,
                )
                break
            except NodePortConflictError as exc:
                last_conflict = exc
                continue

        if started_handle is None:
            if last_conflict is not None:
                raise last_conflict
            raise NodeManagerError(f"No candidate Appium port could restart device {device.id}")

        node.port = started_handle.port
        node.pid = started_handle.pid
        node.active_connection_target = started_handle.active_connection_target or appium_connection_target(device)
        node.health_running = None
        node.health_state = None
        await db.flush()
        return True
    except (AgentCallError, httpx.HTTPError):
        return False
    except NodeManagerError:
        await stop_remote_node(
            port=node.port,
            agent_base=agent_base,
            host=host.ip,
            agent_port=host.agent_port,
            http_client_factory=http_client_factory,
        )
        return False


async def _start_for_node(
    db: AsyncSession,
    device: Device,
    *,
    node: AppiumNode,
    preferred_port: int | None = None,
) -> RemoteStartResult:
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
        # Pack platform missing or unresolved — fall back to no parallel
        # resource allocation. Devices without a pack still start, the
        # allocator simply gets no port/derived-data-path hints.
        pass
    allocated_caps: dict[str, Any] = await appium_node_resource_service.get_capabilities(db, node_id=node.id)
    try:
        short_session = _short_session_factory(db)
        async with short_session() as reserve_db:
            for capability_key, start in resource_ports.items():
                if capability_key in allocated_caps:
                    continue
                allocated_caps[capability_key] = await appium_node_resource_service.reserve(
                    reserve_db,
                    host_id=device.host_id,
                    capability_key=capability_key,
                    start_port=start,
                    node_id=node.id,
                )
            await reserve_db.commit()
    except Exception:
        short_session = _short_session_factory(db)
        async with short_session() as cleanup_db:
            await appium_node_resource_service.release_managed(cleanup_db, node_id=node.id)
            await cleanup_db.commit()
        raise

    if needs_derived_data_path and "appium:derivedDataPath" not in allocated_caps:
        allocated_caps["appium:derivedDataPath"] = f"/tmp/gridfleet/derived-data/{uuid.uuid4().hex}"

    agent_base = await agent_url(device)
    try:
        last_conflict: NodePortConflictError | None = None
        for port in await candidate_ports(db, host_id=device.host_id, preferred_port=preferred_port):
            try:
                short_session = _short_session_factory(db)
                async with short_session() as reserve_db:
                    await reserve_appium_port(
                        reserve_db,
                        host_id=device.host_id,
                        port=port,
                        node_id=node.id,
                    )
                    await reserve_db.commit()
                handle = await start_remote_node(
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
                short_session = _short_session_factory(db)
                async with short_session() as cleanup_db:
                    await appium_node_resource_service.release_capability(
                        cleanup_db,
                        node_id=node.id,
                        capability_key=APPIUM_PORT_CAPABILITY,
                    )
                    await cleanup_db.commit()
                logger.warning(
                    "Managed Appium port conflict for device %s on port %d; trying next candidate",
                    device.id,
                    port,
                )
        else:
            assert last_conflict is not None
            raise last_conflict
    except Exception:
        short_session = _short_session_factory(db)
        async with short_session() as cleanup_db:
            await appium_node_resource_service.release_managed(cleanup_db, node_id=node.id)
            await cleanup_db.commit()
        raise
    handle.allocated_caps = allocated_caps
    return handle


async def start_node(
    db: AsyncSession,
    device: Device,
    *,
    caller: DesiredStateCaller = "operator_route",
) -> AppiumNode:
    await db.refresh(device, attribute_names=["appium_node"])
    if device.appium_node and device.appium_node.observed_running:
        raise NodeManagerError(f"Node already running for device {device.id}")
    if caller != "verification" and not await is_ready_for_use_async(db, device):
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
        target=AppiumDesiredState.running,
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
        target=AppiumDesiredState.stopped,
        caller=caller,
    )
    await db.commit()
    await db.refresh(node)
    return node


async def wait_for_node_running(
    db: AsyncSession,
    node_id: uuid.UUID,
    *,
    timeout_sec: int,
    poll_interval_sec: float = 0.5,
) -> AppiumNode | None:
    """Poll until an AppiumNode reaches observed running state."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        node = await db.get(AppiumNode, node_id)
        if node is not None:
            await db.refresh(node)
            if node.observed_running:
                return node
        await asyncio.sleep(poll_interval_sec)
    return None


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
        target=AppiumDesiredState.running,
        caller=caller,
        desired_port=node.port,
        transition_token=uuid.uuid4(),
        transition_deadline=datetime.now(UTC) + timedelta(seconds=window_sec),
    )
    await db.commit()
    await db.refresh(node)
    return node


async def _clear_manual_recovery_suppression(db: AsyncSession, device_id: uuid.UUID) -> None:
    # Use _hold_device_row_lock to translate NoResultFound (e.g. concurrent
    # device delete after mark_node_started commits and releases the row lock)
    # into NodeManagerError so the route returns a managed 4xx instead of 500.
    locked = await _hold_device_row_lock(db, device_id)
    current_state = lifecycle_policy_state(locked)
    if (
        current_state.get("recovery_suppressed_reason") is None
        and current_state.get("last_failure_reason") is None
        and current_state.get("backoff_until") is None
    ):
        return
    record_manual_recovered(current_state)
    write_lifecycle_policy_state(locked, current_state)
    await db.commit()
