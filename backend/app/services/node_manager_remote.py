from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import httpx

from app.errors import AgentCallError
from app.models.appium_node import AppiumNode, NodeState
from app.services import appium_resource_allocator
from app.services.agent_operations import appium_start, appium_status, appium_stop, response_json_dict
from app.services.device_identity import appium_connection_target
from app.services.node_manager_common import (
    build_appium_driver_caps,
    build_grid_stereotype_caps,
    get_default_plugins,
)
from app.services.node_manager_types import NodeManagerError, NodePortConflictError, TemporaryNodeHandle
from app.services.pack_capability_service import (
    render_default_capabilities,
    render_device_field_capabilities,
    render_stereotype,
)
from app.services.pack_platform_catalog import device_is_virtual
from app.services.pack_platform_resolver import assert_runnable
from app.services.pack_platform_resolver import resolve_pack_platform as resolve_pack_platform_fn
from app.services.pack_start_shim import PackStartPayloadError, build_pack_start_payload, resolve_pack_for_device
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.agent_client import AgentClientFactory
    from app.models.device import Device
    from app.models.host import Host

AVD_LAUNCH_HTTP_TIMEOUT_SECS = 190


def require_management_host(device: Device, *, action: str = "use remote management") -> Host:
    host = device.host
    if host is None or device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned — cannot {action}")
    return host


async def agent_url(device: Device) -> str:
    host = require_management_host(device)
    return f"http://{host.ip}:{host.agent_port}"


def _build_device_owner_key(device: Device) -> str:
    if device.id is None:
        return appium_resource_allocator.temporary_owner_key(device)
    return appium_resource_allocator.managed_owner_key(device.id)


async def _wait_for_remote_appium_ready(
    host: Host,
    *,
    port: int,
    http_client_factory: AgentClientFactory,
    stabilization_timeout_sec: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + stabilization_timeout_sec

    while True:
        payload = await appium_status(
            host.ip,
            host.agent_port,
            port,
            http_client_factory=http_client_factory,
        )
        if payload is not None and payload.get("running") is True:
            return
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0.25)

    raise NodeManagerError(f"Agent reported node start, but Appium is not reachable on port {port}")


def build_agent_start_payload(
    device: Device,
    port: int,
    *,
    allocated_caps: dict[str, Any] | None = None,
    extra_caps: dict[str, Any] | None = None,
) -> dict[str, Any]:
    headless = (device.tags or {}).get("emulator_headless", "true") != "false"
    manager_owned_keys = appium_resource_allocator.manager_owned_cap_keys(frozenset((allocated_caps or {}).keys()))
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
        "stereotype_caps": build_grid_stereotype_caps(
            device,
            session_caps=allocated_caps,
            extra_caps=extra_caps,
            manager_owned_keys=manager_owned_keys,
        )
        or None,
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
    resolved_plat = await resolve_pack_platform_fn(
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


async def start_remote_temporary_node(
    db: AsyncSession,
    device: Device,
    *,
    port: int,
    allocated_caps: dict[str, Any] | None,
    agent_base: str,
    http_client_factory: AgentClientFactory,
) -> TemporaryNodeHandle:
    await assert_runnable(db, pack_id=device.pack_id, platform_id=device.platform_id)
    host = require_management_host(device, action="start Appium nodes")

    # Resolve stereotype once for both consumers (_build_session_aligned_start_caps
    # and build_pack_start_payload) to avoid duplicate DB queries.
    resolved_pack = resolve_pack_for_device(device)
    if resolved_pack is None:
        raise NodeManagerError(f"Device {device.id} has no driver pack platform")
    stereotype = await render_stereotype(db, pack_id=resolved_pack[0], platform_id=resolved_pack[1])
    plat = await resolve_pack_platform_fn(db, pack_id=resolved_pack[0], platform_id=resolved_pack[1])
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
            payload=payload,
            http_client_factory=http_client_factory,
            timeout=_agent_start_timeout(device),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.json().get("detail", str(exc)) if exc.response else str(exc)
        message = f"Agent failed to start node: {detail}"
        lowered = detail.lower()
        if "already in use" in lowered or "already running on port" in lowered:
            raise NodePortConflictError(message) from exc
        raise NodeManagerError(message) from exc
    except AgentCallError:
        raise
    except httpx.HTTPError as exc:
        raise NodeManagerError(f"Cannot reach agent at {agent_base}: {exc}") from exc

    data = response_json_dict(resp)
    try:
        await _wait_for_remote_appium_ready(
            host,
            port=port,
            http_client_factory=http_client_factory,
        )
    except Exception:
        await stop_remote_temporary_node(
            port=port,
            agent_base=agent_base,
            http_client_factory=http_client_factory,
        )
        raise
    active_connection_target = data.get("connection_target")
    return TemporaryNodeHandle(
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
    manager_owned_keys = appium_resource_allocator.manager_owned_cap_keys(frozenset((allocated_caps or {}).keys()))
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


async def stop_remote_temporary_node(
    *,
    port: int,
    agent_base: str,
    http_client_factory: AgentClientFactory,
) -> None:
    try:
        resp = await appium_stop(
            agent_base,
            host=agent_base,
            port=port,
            http_client_factory=http_client_factory,
        )
        resp.raise_for_status()
    except (AgentCallError, httpx.HTTPError):
        return


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
    from app.services import appium_node_locking, device_locking

    await assert_runnable(db, pack_id=device.pack_id, platform_id=device.platform_id)
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
    allocated_caps = await appium_resource_allocator.get_owner_capabilities(
        db,
        appium_resource_allocator.managed_owner_key(device.id),
    )
    # Resolve stereotype once for both consumers.
    resolved_pack = resolve_pack_for_device(device)
    if resolved_pack is None:
        raise NodeManagerError(f"Device {device.id} has no driver pack platform")
    restart_stereotype = await render_stereotype(db, pack_id=resolved_pack[0], platform_id=resolved_pack[1])

    try:
        restart_plat = await resolve_pack_platform_fn(db, pack_id=resolved_pack[0], platform_id=resolved_pack[1])
        restart_appium_platform_name = restart_plat.appium_platform_name
        extra_caps = await _build_session_aligned_start_caps(
            db,
            device,
            allocated_caps=allocated_caps,
            stereotype=restart_stereotype,
            appium_platform_name=restart_appium_platform_name,
        )
        restart_payload = build_agent_start_payload(
            device,
            node.port,
            allocated_caps=allocated_caps,
            extra_caps=extra_caps,
        )
        await _merge_appium_default_pack_caps(db, device, restart_payload)
        try:
            restart_pack_overrides = await build_pack_start_payload(db, device=device, stereotype=restart_stereotype)
        except PackStartPayloadError as exc:
            raise NodeManagerError(str(exc)) from exc
        if restart_pack_overrides is not None:
            restart_payload["pack_id"] = restart_pack_overrides["pack_id"]
            restart_payload["platform_id"] = restart_pack_overrides["platform_id"]
            restart_payload["appium_platform_name"] = restart_pack_overrides["appium_platform_name"]
            restart_payload["stereotype_caps"] = {
                **(restart_payload.get("stereotype_caps") or {}),
                **restart_pack_overrides["stereotype_caps"],
            }
            if "grid_slots" in restart_pack_overrides:
                restart_payload["grid_slots"] = restart_pack_overrides["grid_slots"]
            for key in ("lifecycle_actions", "connection_behavior", "insecure_features", "workaround_env"):
                if key in restart_pack_overrides:
                    restart_payload[key] = restart_pack_overrides[key]
        await appium_stop(
            agent_base,
            host=host.ip,
            port=node.port,
            http_client_factory=http_client_factory,
        )
        await asyncio.sleep(2)
        resp = await appium_start(
            agent_base,
            host=host.ip,
            payload=restart_payload,
            http_client_factory=http_client_factory,
            timeout=_agent_start_timeout(device),
        )
        resp.raise_for_status()
        data = response_json_dict(resp)
        await _wait_for_remote_appium_ready(
            host,
            port=node.port,
            http_client_factory=http_client_factory,
        )
        node.pid = data.get("pid")
        active_connection_target = data.get("connection_target")
        node.active_connection_target = active_connection_target if isinstance(active_connection_target, str) else None
        node.state = NodeState.running
        return True
    except (AgentCallError, httpx.HTTPError):
        return False
    except NodeManagerError:
        await stop_remote_temporary_node(
            port=node.port,
            agent_base=agent_base,
            http_client_factory=http_client_factory,
        )
        return False
