"""Single-module node lifecycle service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from datetime import datetime

    from app.appium_nodes.protocols import OperatorNodeManager
    from app.core.protocols import SettingsReader

from sqlalchemy import inspect as sqlalchemy_inspect

from app.appium_nodes.exceptions import NodeManagerError, NodePortConflictError
from app.appium_nodes.models import AppiumNode
from app.appium_nodes.services import (
    capability_keys as appium_capability_keys,
)
from app.appium_nodes.services import (
    resource_service as appium_node_resource_service,
)
from app.appium_nodes.services.common import (
    build_appium_driver_caps,
    node_state_severity,
)
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity import appium_connection_target
from app.devices.services.readiness import is_ready_for_use_async, readiness_error_detail_async
from app.lifecycle.services.actions import reset_reconciler_start_failure_if_needed
from app.packs.services import capability as pack_capability
from app.packs.services import platform_resolver as pack_platform_resolver
from app.packs.services import start_shim as pack_start_shim

assert_runnable = pack_platform_resolver.assert_runnable
build_device_context = pack_start_shim.build_device_context
build_pack_start_payload = pack_start_shim.build_pack_start_payload
render_default_capabilities = pack_capability.render_default_capabilities
render_device_field_capabilities = pack_capability.render_device_field_capabilities
render_stereotype = pack_capability.render_stereotype
resolve_pack_for_device = pack_start_shim.resolve_pack_for_device
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform
PackStartPayloadError = pack_start_shim.PackStartPayloadError

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.protocols import SettingsReader
    from app.devices.locking import LockedDevice
    from app.devices.models import Device
    from app.devices.services.decision_snapshot import DeviceDecisionSnapshot
    from app.events.protocols import EventPublisher
    from app.hosts.models import Host

RESTART_BACKOFF_BASE = 2


RESTART_MAX_RETRIES = 3


@dataclass(frozen=True, slots=True)
class NodeStartDetails:
    """Optional refinements applied when recording a node start beyond port/pid."""

    active_connection_target: str | None = None
    allocated_caps: dict[str, Any] | None = None
    started_at: datetime | None = None
    pack_release: str | None = None


__all__ = [
    "RESTART_BACKOFF_BASE",
    "RESTART_MAX_RETRIES",
    "NodeManagerError",
    "NodePortConflictError",
    "NodeStartDetails",
    "ReconcilerAgentService",
    "build_agent_start_payload",
    "build_node_launch_payload",
    "mark_node_started",
    "mark_node_stopped",
    "require_management_host",
]


def upsert_node(
    db: AsyncSession,
    device: Device,
    port: int,
    pid: int | None,
    active_connection_target: str | None,
    *,
    settings: SettingsReader,
) -> AppiumNode:
    if device.appium_node:
        node = cast("AppiumNode", device.appium_node)
        node.port = port
        node.pid = pid
        node.active_connection_target = active_connection_target
    else:
        node = AppiumNode(
            device_id=device.id,
            port=port,
            pid=pid,
            active_connection_target=active_connection_target,
        )
        db.add(node)
    device.appium_node = node
    return node


async def mark_node_started(  # noqa: PLR0913 - lock proof, locked child, and snapshot all cross the boundary
    db: AsyncSession,
    locked: LockedDevice,
    locked_node: AppiumNode | None,
    snapshot: DeviceDecisionSnapshot,
    *,
    port: int,
    pid: int | None,
    details: NodeStartDetails,
    publisher: EventPublisher,
    settings: SettingsReader,
) -> DeviceDecisionSnapshot:
    """Fold one observed node start under the caller's Device lock. Flush only."""
    device = locked.device
    active_target = details.active_connection_target or appium_connection_target(device)
    node = locked_node or upsert_node(db, device, port, pid, active_target, settings=settings)
    node.port = port
    node.pid = pid
    node.active_connection_target = active_target
    if details.started_at is not None:
        node.started_at = details.started_at
    if details.pack_release is not None:
        node.observed_pack_release = details.pack_release
    await db.flush()
    if device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned — cannot promote Appium resource claims")
    for key, value in (details.allocated_caps or {}).items():
        if isinstance(value, int):
            continue
        await appium_node_resource_service.set_node_extra_capability(
            db,
            node_id=node.id,
            capability_key=key,
            value=value,
        )
    # Device-axis restoration stays owned by the health writer's
    # _restore_available_for_healthy_signal; a direct set_operational_state here
    # would flap the device offline whenever transient signals lag the node axis.
    updated_snapshot = await DeviceHealthService(publisher=publisher).apply_locked_node_state_transition(
        db,
        locked,
        node,
        snapshot,
        mark_offline=False,
    )
    await reset_reconciler_start_failure_if_needed(db, device, ladder=snapshot.ladder)
    # Pull re-applies drain/stop flags from the last pull on crash-restart (the
    # agent re-derives accepting_new_sessions/stop_pending from desired state
    # every launch), so the belt-and-suspenders outbox re-push that used to run
    # here for the push path is unnecessary.
    publisher.queue_for_session(
        db,
        "node.state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_state": "stopped",
            "new_state": "running",
            "port": port,
        },
        severity=node_state_severity("stopped", "running"),
    )
    await db.flush()
    return updated_snapshot


async def mark_node_stopped(
    db: AsyncSession,
    locked: LockedDevice,
    locked_node: AppiumNode | None,
    snapshot: DeviceDecisionSnapshot,
    *,
    publisher: EventPublisher,
) -> DeviceDecisionSnapshot:
    """Fold one observed node stop under the caller's Device lock. Flush only."""
    if locked_node is None:
        # The node row went away between the caller's inventory and its lock.
        return snapshot
    device = locked.device
    locked_node.pid = None
    locked_node.active_connection_target = None
    updated_snapshot = await DeviceHealthService(publisher=publisher).apply_locked_node_state_transition(
        db,
        locked,
        locked_node,
        snapshot,
        mark_offline=False,
    )
    publisher.queue_for_session(
        db,
        "node.state_changed",
        {
            "device_id": str(device.id),
            "device_name": device.name,
            "old_state": "running",
            "new_state": "stopped",
        },
        severity=node_state_severity("running", "stopped"),
    )
    await db.flush()
    return updated_snapshot


def require_management_host(device: Device, *, action: str = "use remote management") -> Host:
    host = cast("Host | None", device.host)
    if host is None or device.host_id is None:
        raise NodeManagerError(f"Device {device.id} has no host assigned — cannot {action}")
    return host


def build_agent_start_payload(
    device: Device,
    port: int,
    *,
    allocated_caps: dict[str, Any] | None = None,
    extra_caps: dict[str, Any] | None = None,
    settings: SettingsReader,
) -> dict[str, Any]:
    manager_owned_keys = appium_capability_keys.manager_owned_cap_keys(frozenset((allocated_caps or {}).keys()))
    node = (
        None if "appium_node" in sqlalchemy_inspect(device).unloaded else cast("AppiumNode | None", device.appium_node)
    )
    accepting_new_sessions = node.accepting_new_sessions if node is not None else True
    stop_pending = node.stop_pending if node is not None else False
    grid_run_id = node.desired_grid_run_id if node is not None else None
    return {
        "connection_target": appium_connection_target(device),
        "platform_id": device.platform_id,
        "port": port,
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
        "accepting_new_sessions": accepting_new_sessions,
        "stop_pending": stop_pending,
        "grid_run_id": str(grid_run_id) if grid_run_id else None,
        "device_type": device.device_type.value,
        "ip_address": device.ip_address,
        "allocated_caps": allocated_caps or None,
        # ponytail: never flipped in production; re-add a registry row if a
        # driver pack ever needs lingering sessions preserved.
        "session_override": True,
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


async def build_node_launch_payload(
    db: AsyncSession,
    device: Device,
    *,
    port: int,
    allocated_caps: dict[str, Any] | None,
    settings: SettingsReader,
) -> dict[str, Any]:
    """Build the complete launch payload shared by push and pull channels."""
    await assert_runnable(db, pack_id=device.pack_id, platform_id=device.platform_id)
    host = require_management_host(device, action="start Appium nodes")

    # Resolve stereotype once for both consumers (_build_session_aligned_start_caps
    # and build_pack_start_payload) to avoid duplicate DB queries.
    resolved_pack = resolve_pack_for_device(device)
    if resolved_pack is None:
        raise NodeManagerError(f"Device {device.id} has no driver pack platform")
    # Entry-time read of the selected release. Every release-owned field below
    # (stereotype, platform data, appium_env, insecure_features) is loaded in
    # its own query under READ COMMITTED, so a concurrent release switch can
    # tear them. The stamp computed at the end must match this read or the
    # payload is refused — the agent skips the tick and the next poll derives
    # everything from the new release.
    entry_release = await pack_start_shim.selected_release_id(db, resolved_pack[0])
    stereotype = await render_stereotype(
        db,
        pack_id=resolved_pack[0],
        platform_id=resolved_pack[1],
        device_context=build_device_context(device),
    )
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
        settings=settings,
    )
    await _merge_appium_default_pack_caps(db, device, payload)
    try:
        pack_overrides = await build_pack_start_payload(db, device=device, stereotype=stereotype)
    except PackStartPayloadError as exc:
        raise NodeManagerError(str(exc)) from exc
    if pack_overrides is not None and pack_overrides.get("pack_release") != entry_release:
        raise NodeManagerError(
            f"pack {resolved_pack[0]!r} release changed while building the launch payload; retrying next poll"
        )
    if pack_overrides is not None:
        payload["pack_id"] = pack_overrides["pack_id"]
        payload["platform_id"] = pack_overrides["platform_id"]
        payload["appium_platform_name"] = pack_overrides["appium_platform_name"]
        for key in ("pack_release", "lifecycle_actions", "connection_behavior", "insecure_features"):
            if key in pack_overrides:
                payload[key] = pack_overrides[key]
        # Merge host tool_env (operator per-host config) under pack appium_env
        # (pack-specific fixes). Pack appium_env wins for duplicate keys.
        pack_appium_env = pack_overrides.get("appium_env") or {}
        if host.tool_env or pack_appium_env:
            merged_env = dict(host.tool_env or {})
            merged_env.update(pack_appium_env)
            payload["appium_env"] = merged_env
    elif host.tool_env:
        # No pack overrides, but host provides tool_env — pass it through.
        payload["appium_env"] = dict(host.tool_env)
    return payload


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


class ReconcilerAgentService:
    def __init__(self, *, settings: SettingsReader, operator: OperatorNodeManager) -> None:
        self._settings = settings
        self._operator = operator

    async def start_node(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = "operator_route"
    ) -> AppiumNode:
        """Operator-initiated single-device start.

        Routes through ``self._operator.request_start`` so the operator:start
        intent payload is the single source of truth. Direct ``write_desired_state``
        calls are forbidden in operator code paths.
        """
        await db.refresh(device, attribute_names=["appium_node"])
        if device.appium_node and device.appium_node.observed_running:
            raise NodeManagerError(f"Node already running for device {device.id}")
        if caller != "verification" and not await is_ready_for_use_async(db, device):
            raise NodeManagerError(await readiness_error_detail_async(db, device, action="start a node"))

        node = await self._operator.request_start(db, device, caller=caller, reason=f"{caller} start requested")
        await db.commit()
        await db.refresh(node)
        return node

    async def stop_node(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = "operator_route"
    ) -> AppiumNode:
        """Operator-initiated single-device stop. Routes through
        ``self._operator.request_stop`` so operator:stop intents are the
        single source of truth.
        """
        node = cast("AppiumNode | None", device.appium_node)
        if not node or not node.observed_running:
            raise NodeManagerError(f"No running node for device {device.id}")

        node = await self._operator.request_stop(db, device, reason=f"{caller} stop requested")
        await db.commit()
        await db.refresh(node)
        return node

    async def restart_node(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = "operator_restart"
    ) -> AppiumNode:
        """Operator-initiated single-device restart. Routes through
        ``self._operator.request_restart`` so the operator:start intent
        payload is the single source of truth (with a fresh restart_requested_at
        watermark and expires_at on every restart).
        """
        if not device.appium_node or not device.appium_node.observed_running:
            return await self.start_node(db, device, caller=caller)

        node = await self._operator.request_restart(db, device, caller=caller, reason=f"{caller} restart requested")
        await db.commit()
        return node
