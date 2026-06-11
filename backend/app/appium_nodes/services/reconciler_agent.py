"""Single-module node lifecycle service."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from app.agent_comm.http_pool import AgentHttpPool
    from app.appium_nodes.protocols import OperatorNodeManager
    from app.core.protocols import SettingsReader

import httpx
from sqlalchemy import inspect as sqlalchemy_inspect
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agent_comm import operations as agent_operations
from app.agent_comm.error_codes import AgentErrorCode
from app.agent_comm.models import AgentReconfigureOutbox
from app.agent_comm.operations import appium_start, appium_stop, parse_agent_error_detail, response_json_dict
from app.appium_nodes.exceptions import (
    NodeAlreadyRunningError,
    NodeManagerError,
    NodePortConflictError,
    RemoteStartResult,
)
from app.appium_nodes.models import AppiumNode
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
    get_default_plugins,
    node_state_severity,
)
from app.appium_nodes.services.reconciler_allocation import (
    APPIUM_PORT_CAPABILITY,
    candidate_ports,
    reserve_appium_port,
)
from app.core.database import async_session
from app.core.errors import AgentCallError
from app.devices import locking as device_locking
from app.devices.services.health import DeviceHealthService
from app.devices.services.identity import appium_connection_target
from app.devices.services.intent import IntentService
from app.devices.services.readiness import is_ready_for_use_async, readiness_error_detail_async
from app.lifecycle.services.actions import (
    reset_reconciler_start_failure_state,
)
from app.packs.services import capability as pack_capability
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver
from app.packs.services import start_shim as pack_start_shim

applicable_resource_ports = pack_platform_resolver.applicable_resource_ports
assert_runnable = pack_platform_resolver.assert_runnable
build_device_context = pack_start_shim.build_device_context
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

    from app.agent_comm.client import AgentClientFactory
    from app.agent_comm.protocols import CircuitBreakerProtocol
    from app.appium_nodes.services.desired_state_writer import DesiredStateCaller
    from app.core.protocols import SettingsReader
    from app.devices.models import Device
    from app.events.protocols import EventPublisher
    from app.hosts.models import Host

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
    "ReconcilerAgentService",
    "RemoteStartResult",
    "agent_url",
    "build_agent_start_payload",
    "candidate_ports",
    "mark_node_started",
    "mark_node_stopped",
    "require_management_host",
    "start_remote_node",
    "stop_remote_node",
]


async def _hold_device_row_lock(db: AsyncSession, device_id: uuid.UUID) -> Device:
    """Acquire and refresh the Device row used for node-state writes."""
    try:
        return await device_locking.lock_device(db, device_id)
    except NoResultFound:
        raise NodeManagerError(f"Device {device_id} no longer exists") from None


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


async def mark_node_started(
    db: AsyncSession,
    device: Device,
    *,
    port: int,
    pid: int | None,
    active_connection_target: str | None = None,
    allocated_caps: dict[str, Any] | None = None,
    clear_transition: bool = False,
    publisher: EventPublisher,
    settings: SettingsReader,
) -> AppiumNode:
    device = await _hold_device_row_lock(db, device.id)
    await appium_node_locking.lock_appium_node_for_device(db, device.id)
    active_connection_target = active_connection_target or appium_connection_target(device)
    node = upsert_node(db, device, port, pid, active_connection_target, settings=settings)
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
    # Device-axis restoration is owned by ``apply_node_state_transition``'s
    # ``_restore_available_for_healthy_signal``, which transitions offline →
    # available iff signals are stably healthy. A direct ``set_operational_state``
    # with the ``ready_operational_state`` projection here would flap the
    # device offline whenever transient signals (stale ``health_running``,
    # ``device_checks_healthy``) lag the node-axis update — operators see
    # spurious "Node started" → offline → "Health checks recovered" toasts.
    await DeviceHealthService(publisher=publisher).apply_node_state_transition(
        db,
        device,
        mark_offline=False,
    )
    reset_reconciler_start_failure_state(device)
    if clear_transition:
        node.transition_token = None
        node.transition_deadline = None
    # Defense in depth for the cooldown / maintenance restart path: a
    # freshly started agent-side relay always constructs ``NodeState``
    # with ``_drain=False``. The agent honors the launch spec's
    # ``accepting_new_sessions=False`` and re-drains the new relay (see
    # ``_start_grid_node_service`` in agent process.py), but if that agent
    # fix regresses or a third-party agent fails to apply it, the new
    # relay would silently accept hub-routed sessions until the next
    # ``device_intent_reconciler_loop`` tick happens to stage a fresh
    # reconfigure — and the dedup check in ``_stage_agent_reconfigure``
    # only stages on metadata change, so a steady-state cooldown intent
    # would never produce a follow-up row on its own. Stage a forced
    # outbox row here whenever the restart lands on a relay that should
    # be drained or stopping; the background delivery loop picks it up
    # within seconds and re-pushes the drain to the new port.
    if node.port is not None and (not node.accepting_new_sessions or node.stop_pending):
        db.add(
            AgentReconfigureOutbox(
                device_id=node.device_id,
                port=node.port,
                accepting_new_sessions=node.accepting_new_sessions,
                stop_pending=node.stop_pending,
                grid_run_id=node.desired_grid_run_id,
                reconciled_generation=node.generation,
            )
        )
        await db.flush()
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
    await db.commit()
    await db.refresh(node)
    return node


async def mark_node_stopped(db: AsyncSession, device: Device, *, publisher: EventPublisher) -> AppiumNode:
    device = await _hold_device_row_lock(db, device.id)
    node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
    assert node is not None
    node.pid = None
    node.active_connection_target = None
    # Mark dirty so the reconciler derives the correct operational state
    # (offline when the node stops, unless a running session holds it busy).
    # Skip the write-through state machine that used to route AUTO_STOP_EXECUTED
    # here: the reconciler's apply_derived_state is now authoritative.
    await IntentService(db).mark_dirty_and_reconcile(device.id, reason="Node stopped", publisher=publisher)
    await DeviceHealthService(publisher=publisher).apply_node_state_transition(
        db,
        device,
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
    settings: SettingsReader,
) -> dict[str, Any]:
    headless = (device.tags or {}).get("emulator_headless", "true") != "false"
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
        "plugins": get_default_plugins(settings=settings) or None,
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
        "session_override": settings.get("appium.session_override"),
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


def _agent_start_timeout(device: Device, *, settings: SettingsReader) -> float | int:
    base = settings.get_int("appium.startup_timeout_sec") + 5
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
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
) -> RemoteStartResult:
    await assert_runnable(db, pack_id=device.pack_id, platform_id=device.platform_id)
    host = require_management_host(device, action="start Appium nodes")

    # Resolve stereotype once for both consumers (_build_session_aligned_start_caps
    # and build_pack_start_payload) to avoid duplicate DB queries.
    resolved_pack = resolve_pack_for_device(device)
    if resolved_pack is None:
        raise NodeManagerError(f"Device {device.id} has no driver pack platform")
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
    if pack_overrides is not None:
        payload["pack_id"] = pack_overrides["pack_id"]
        payload["platform_id"] = pack_overrides["platform_id"]
        payload["appium_platform_name"] = pack_overrides["appium_platform_name"]
        if "grid_slots" in pack_overrides:
            payload["grid_slots"] = pack_overrides["grid_slots"]
        for key in ("lifecycle_actions", "connection_behavior", "insecure_features"):
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
    try:
        resp = await appium_start(
            agent_base,
            host=host.ip,
            agent_port=host.agent_port,
            payload=payload,
            http_client_factory=http_client_factory,
            timeout=_agent_start_timeout(device, settings=settings),
            settings=settings,
            pool=pool,
            circuit_breaker=circuit_breaker,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code, message_text = parse_agent_error_detail(exc.response)
        message = f"Agent failed to start node: {message_text}"
        if code == AgentErrorCode.ALREADY_RUNNING.value:
            # Per-target conflict: a node already runs for this connection target.
            # Retrying on a different candidate port is futile (the agent's guard
            # keys on the target, not the port), so raise the distinct subclass.
            raise NodeAlreadyRunningError(message) from exc
        if code == AgentErrorCode.PORT_OCCUPIED.value:
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
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
) -> bool:
    """Ask the agent to stop the Appium node on ``port``.

    Returns True on confirmed agent acknowledgement, False otherwise. Callers
    that mutate DB node state on the back of a stop MUST gate that mutation on
    True — a False return means we cannot prove the agent process is gone, and
    flipping the DB to ``stopped`` would leave the agent's Appium process
    orphaned and still reachable by the router on its allocated port.
    """
    try:
        resp = await appium_stop(
            agent_base,
            host=host,
            agent_port=agent_port,
            port=port,
            http_client_factory=http_client_factory,
            settings=settings,
            pool=pool,
            circuit_breaker=circuit_breaker,
        )
        resp.raise_for_status()
        return True
    except (AgentCallError, httpx.HTTPError):
        return False


async def _start_for_node(
    db: AsyncSession,
    device: Device,
    *,
    node: AppiumNode,
    preferred_port: int | None = None,
    settings: SettingsReader,
    circuit_breaker: CircuitBreakerProtocol,
    pool: AgentHttpPool | None = None,
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
        resource_ports = {p.capability_name: p.start for p in applicable_resource_ports(resolved, device.device_config)}
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
        for port in await candidate_ports(db, host_id=device.host_id, preferred_port=preferred_port, settings=settings):
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
                    settings=settings,
                    pool=pool,
                    circuit_breaker=circuit_breaker,
                )
                break
            except NodeAlreadyRunningError:
                # Per-target conflict — every candidate port hits the same
                # agent-side guard, so stop iterating and re-raise. The outer
                # ``except`` releases the reserved port via ``release_managed``;
                # the caller treats this as already-converged.
                raise
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

        node = await self._operator.request_stop(db, device, caller=caller, reason=f"{caller} stop requested")
        await db.commit()
        await db.refresh(node)
        return node

    async def restart_node(
        self, db: AsyncSession, device: Device, *, caller: DesiredStateCaller = "operator_restart"
    ) -> AppiumNode:
        """Operator-initiated single-device restart. Routes through
        ``self._operator.request_restart`` so the operator:start intent
        payload is the single source of truth (with fresh transition_token and
        expires_at on every restart).
        """
        if not device.appium_node or not device.appium_node.observed_running:
            return await self.start_node(db, device, caller=caller)

        node = await self._operator.request_restart(db, device, caller=caller, reason=f"{caller} restart requested")
        await db.commit()
        await db.refresh(node)
        return node

    async def wait_for_node_running(
        self, db: AsyncSession, node_id: uuid.UUID, *, timeout_sec: int, poll_interval_sec: float = 0.5
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
