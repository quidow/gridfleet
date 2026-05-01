import asyncio
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.errors import AgentCallError
from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.models.device_event import DeviceEventType
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop
from app.services import control_plane_state_store, device_health_summary, device_locking, lifecycle_policy
from app.services.agent_operations import (
    get_pack_devices,
    pack_device_lifecycle_action,
)
from app.services.agent_operations import (
    pack_device_health as fetch_pack_device_health,
)
from app.services.device_availability import set_device_availability_status
from app.services.device_event_service import record_event
from app.services.device_readiness import is_ready_for_use_async
from app.services.node_manager_remote import stop_node_via_agent as stop_node_via_agent_helper
from app.services.pack_platform_catalog import platform_has_lifecycle_action
from app.services.pack_platform_resolver import resolve_pack_platform
from app.services.settings_service import settings_service

logger = get_logger(__name__)
CONNECTIVITY_NAMESPACE = "connectivity.previously_offline"
LOOP_NAME = "device_connectivity"
ACTIVE_STATES = {
    DeviceAvailabilityStatus.busy,
    DeviceAvailabilityStatus.reserved,
    DeviceAvailabilityStatus.maintenance,
}


def _add_avd_aliases(aliases: set[str], value: str) -> None:
    if value.startswith("avd:"):
        aliases.add(value.removeprefix("avd:"))
    elif value and not value.startswith("emulator-"):
        aliases.add(f"avd:{value}")


def _agent_device_aliases(device: dict[str, Any]) -> set[str]:
    aliases = {
        value
        for value in (device.get("connection_target"), device.get("identity_value"))
        if isinstance(value, str) and value
    }
    detected = device.get("detected_properties")
    if isinstance(detected, dict):
        avd_name = detected.get("avd_name")
        if isinstance(avd_name, str) and avd_name:
            aliases.add(avd_name)
            aliases.add(f"avd:{avd_name}")
    for alias in list(aliases):
        if alias.startswith("avd:"):
            _add_avd_aliases(aliases, alias)
    return aliases


def _device_expected_aliases(device: Device) -> set[str]:
    aliases = {value for value in (device.connection_target, device.identity_value) if isinstance(value, str) and value}
    if device.device_type == DeviceType.emulator:
        for alias in list(aliases):
            _add_avd_aliases(aliases, alias)
    return aliases


async def _get_agent_devices(host: Host) -> set[str] | None:
    """Fetch connected device targets from the host agent. Returns None if unreachable."""
    try:
        pack_result = await get_pack_devices(
            host.ip,
            host.agent_port,
            http_client_factory=httpx.AsyncClient,
        )
        candidates = pack_result.get("candidates", [])
        if not isinstance(candidates, list):
            return set()
        aliases: set[str] = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            detected = candidate.get("detected_properties", {})
            if not isinstance(detected, dict):
                detected = {}
            # Build a device-like dict for alias extraction
            device_like: dict[str, Any] = {
                "connection_target": detected.get("connection_target") or candidate.get("identity_value"),
                "identity_value": candidate.get("identity_value"),
                "detected_properties": detected,
            }
            aliases.update(_agent_device_aliases(device_like))
        return aliases
    except AgentCallError:
        return None


async def _get_device_health(device: Device) -> dict[str, Any] | None:
    host = device.host
    if host is None or device.connection_target is None:
        return None

    try:
        return await fetch_pack_device_health(
            host.ip,
            host.agent_port,
            device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else "real_device",
            connection_type=device.connection_type.value if device.connection_type else None,
            ip_address=device.ip_address,
            http_client_factory=httpx.AsyncClient,
        )
    except AgentCallError:
        return None


async def _uses_endpoint_health(db: AsyncSession, device: Device) -> bool:
    try:
        resolved = await resolve_pack_platform(
            db,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else None,
        )
    except LookupError:
        return False
    return (
        resolved.connection_behavior.get("requires_connection_target") is False
        and device.connection_type == ConnectionType.network
        and bool(device.ip_address or device.connection_target)
    )


async def _get_lifecycle_state(db: AsyncSession, device: Device) -> str | None:
    """Poll the agent for the pack-owned lifecycle state."""
    try:
        resolved = await resolve_pack_platform(
            db,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else None,
        )
    except LookupError:
        return None
    if not platform_has_lifecycle_action(resolved.lifecycle_actions, "state"):
        return None
    host = device.host
    if host is None or device.connection_target is None:
        return None

    try:
        result = await pack_device_lifecycle_action(
            host.ip,
            host.agent_port,
            device.connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            action="state",
            http_client_factory=httpx.AsyncClient,
        )
    except AgentCallError:
        return None

    state = result.get("state")
    return str(state) if isinstance(state, str) and state else None


def _summarize_unhealthy_result(result: dict[str, Any] | None) -> str:
    if not isinstance(result, dict):
        return "Device health checks failed"
    detail = result.get("detail")
    if isinstance(detail, str) and detail:
        return detail

    checks = result.get("checks")
    if isinstance(checks, list):
        failures = [
            c.get("check_id", "unknown").replace("_", " ") for c in checks if isinstance(c, dict) and not c.get("ok")
        ]
        return f"Failed checks: {', '.join(failures)}" if failures else "Device health checks failed"

    return "Device health checks failed"


async def _stop_node_via_agent(device: Device, node: AppiumNode) -> bool:
    """Stop an Appium node through the host agent."""
    return await stop_node_via_agent_helper(device, node, http_client_factory=httpx.AsyncClient)


async def _stop_disconnected_node(db: AsyncSession, device: Device) -> bool | None:
    node = device.appium_node
    if node is None or node.state == NodeState.stopped:
        return None

    stopped = await _stop_node_via_agent(device, node)
    if stopped:
        node.state = NodeState.stopped
        node.pid = None
    else:
        node.state = NodeState.error
    await device_health_summary.update_node_state(db, device, running=False, state=node.state.value)
    return stopped


async def reset_connectivity_control_plane_state(db: AsyncSession) -> None:
    await control_plane_state_store.delete_namespace(db, CONNECTIVITY_NAMESPACE)
    await db.commit()


async def get_connectivity_control_plane_state(db: AsyncSession) -> set[str]:
    return set((await control_plane_state_store.get_values(db, CONNECTIVITY_NAMESPACE)).keys())


async def track_previously_offline_device(db: AsyncSession, identity_value: str) -> None:
    await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, identity_value, True)
    await db.commit()


async def _check_connectivity(db: AsyncSession) -> None:
    stmt = select(Host).where(Host.status == HostStatus.online)
    result = await db.execute(stmt)
    hosts = result.scalars().all()

    for host in hosts:
        # Get registered devices for this host
        device_stmt = select(Device).where(Device.host_id == host.id).options(selectinload(Device.appium_node))
        device_result = await db.execute(device_stmt)
        devices = device_result.scalars().all()

        connected_targets = await _get_agent_devices(host)
        if connected_targets is None:
            continue  # Agent unreachable — skip (heartbeat handles host status)

        for device in devices:
            lifecycle_state = await _get_lifecycle_state(db, device)
            if lifecycle_state is not None:
                await device_health_summary.patch_health_snapshot(db, device, {"emulator_state": lifecycle_state})

            if _device_expected_aliases(device) & connected_targets:
                # Device is connected
                health_result = await _get_device_health(device)
                if health_result is not None and not health_result.get("healthy", False):
                    await device_health_summary.update_device_checks(
                        db,
                        device,
                        healthy=False,
                        summary=_summarize_unhealthy_result(health_result),
                    )
                    await lifecycle_policy.handle_health_failure(
                        db,
                        device,
                        source="device_checks",
                        reason=_summarize_unhealthy_result(health_result),
                    )
                    await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, device.identity_value, True)
                    continue
                if health_result is not None:
                    summary = (
                        "Healthy" if health_result.get("healthy", False) else _summarize_unhealthy_result(health_result)
                    )
                    await device_health_summary.update_device_checks(
                        db,
                        device,
                        healthy=bool(health_result.get("healthy", False)),
                        summary=summary,
                    )

                if device.availability_status == DeviceAvailabilityStatus.offline:
                    if not await is_ready_for_use_async(db, device):
                        logger.debug("Device %s is connected but still awaiting setup/verification", device.name)
                        await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
                        continue
                    if not device.auto_manage:
                        logger.debug("Device %s is connected but auto_manage is off — skipping auto-start", device.name)
                        await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
                        continue
                    previously_offline = await control_plane_state_store.get_value(
                        db,
                        CONNECTIVITY_NAMESPACE,
                        device.identity_value,
                    )
                    restored = await lifecycle_policy.attempt_auto_recovery(
                        db,
                        device,
                        source="device_checks",
                        reason=(
                            "Device reconnected and passed health checks"
                            if previously_offline
                            else "Startup recovery after healthy reconnect"
                        ),
                    )
                    if restored:
                        await control_plane_state_store.delete_value(db, CONNECTIVITY_NAMESPACE, device.identity_value)
                    else:
                        await control_plane_state_store.set_value(
                            db, CONNECTIVITY_NAMESPACE, device.identity_value, True
                        )
            else:
                if await _uses_endpoint_health(db, device):
                    health_result = await _get_device_health(device)
                    if health_result is not None and health_result.get("healthy", False):
                        await device_health_summary.update_device_checks(db, device, healthy=True, summary="Healthy")
                        if device.availability_status == DeviceAvailabilityStatus.offline:
                            if not await is_ready_for_use_async(db, device):
                                logger.debug("Device %s is healthy but still awaiting setup/verification", device.name)
                                await control_plane_state_store.delete_value(
                                    db, CONNECTIVITY_NAMESPACE, device.identity_value
                                )
                                continue
                            if not device.auto_manage:
                                logger.debug(
                                    "Device %s is healthy but auto_manage is off — skipping auto-start",
                                    device.name,
                                )
                                await control_plane_state_store.delete_value(
                                    db, CONNECTIVITY_NAMESPACE, device.identity_value
                                )
                                continue
                            previously_offline = await control_plane_state_store.get_value(
                                db,
                                CONNECTIVITY_NAMESPACE,
                                device.identity_value,
                            )
                            restored = await lifecycle_policy.attempt_auto_recovery(
                                db,
                                device,
                                source="device_checks",
                                reason=(
                                    "Device reconnected and passed endpoint health checks"
                                    if previously_offline
                                    else "Startup recovery after healthy endpoint check"
                                ),
                            )
                            if restored:
                                await control_plane_state_store.delete_value(
                                    db, CONNECTIVITY_NAMESPACE, device.identity_value
                                )
                            else:
                                await control_plane_state_store.set_value(
                                    db, CONNECTIVITY_NAMESPACE, device.identity_value, True
                                )
                        else:
                            await control_plane_state_store.delete_value(
                                db, CONNECTIVITY_NAMESPACE, device.identity_value
                            )
                        continue
                # Device disconnected
                if not device.auto_manage:
                    continue
                stopped_node = await _stop_disconnected_node(db, device)
                if device.availability_status == DeviceAvailabilityStatus.offline:
                    if stopped_node is not None:
                        await control_plane_state_store.set_value(
                            db,
                            CONNECTIVITY_NAMESPACE,
                            device.identity_value,
                            True,
                        )
                    continue
                if device.availability_status in ACTIVE_STATES:
                    logger.warning(
                        "Device %s (%s) appears disconnected on host %s but is %s — leaving status unchanged",
                        device.name,
                        device.identity_value,
                        host.hostname,
                        device.availability_status.value,
                    )
                    await record_event(
                        db,
                        device.id,
                        DeviceEventType.connectivity_lost,
                        {"reason": "Device disconnected (kept active state)"},
                    )
                    await device_health_summary.update_device_checks(db, device, healthy=False, summary="Disconnected")
                    locked_device = await device_locking.lock_device(db, device.id)
                    if locked_device.availability_status in ACTIVE_STATES:
                        await lifecycle_policy.note_connectivity_loss(db, locked_device, reason="Device disconnected")
                        await control_plane_state_store.set_value(
                            db, CONNECTIVITY_NAMESPACE, locked_device.identity_value, True
                        )
                    else:
                        logger.info(
                            "Device %s (%s) left active state before lifecycle write — skipping",
                            locked_device.name,
                            locked_device.identity_value,
                        )
                    continue
                logger.warning(
                    "Device %s (%s) disconnected from host %s",
                    device.name,
                    device.identity_value,
                    host.hostname,
                )
                await record_event(
                    db,
                    device.id,
                    DeviceEventType.connectivity_lost,
                    {"reason": "Device disconnected"},
                )
                await device_health_summary.update_device_checks(db, device, healthy=False, summary="Disconnected")
                locked_device = await device_locking.lock_device(db, device.id)
                if locked_device.availability_status not in ACTIVE_STATES:
                    await set_device_availability_status(
                        locked_device,
                        DeviceAvailabilityStatus.offline,
                        reason="Device disconnected",
                    )
                    await lifecycle_policy.note_connectivity_loss(db, locked_device, reason="Device disconnected")
                    await control_plane_state_store.set_value(
                        db, CONNECTIVITY_NAMESPACE, locked_device.identity_value, True
                    )
                else:
                    logger.info(
                        "Device %s (%s) transitioned to %s before offline write — skipping",
                        locked_device.name,
                        locked_device.identity_value,
                        locked_device.availability_status.value,
                    )

    await db.commit()


async def device_connectivity_loop() -> None:
    """Background loop that checks device connectivity via host agents."""
    while True:
        interval = float(settings_service.get("general.device_check_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _check_connectivity(db)
        except Exception:
            logger.exception("Device connectivity check failed")
        await asyncio.sleep(interval)
