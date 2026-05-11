import asyncio
import os
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import metrics
from app.database import async_session
from app.errors import AgentCallError
from app.models.appium_node import NodeState
from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop
from app.services import appium_node_locking, control_plane_state_store, device_health, device_locking, lifecycle_policy
from app.services.agent_operations import (
    get_pack_devices,
    pack_device_lifecycle_action,
)
from app.services.agent_operations import (
    pack_device_health as fetch_pack_device_health,
)
from app.services.control_plane_leader import LeadershipLost, assert_current_leader
from app.services.desired_state_writer import write_desired_state
from app.services.device_readiness import is_ready_for_use_async
from app.services.device_state import legacy_label_for_audit
from app.services.lifecycle_state_machine import DeviceStateMachine
from app.services.lifecycle_state_machine_hooks import EventLogHook, IncidentHook, RunExclusionHook
from app.services.lifecycle_state_machine_types import TransitionEvent
from app.services.pack_platform_catalog import platform_has_lifecycle_action
from app.services.pack_platform_resolver import resolve_pack_platform
from app.services.settings_service import settings_service

logger = get_logger(__name__)
CONNECTIVITY_NAMESPACE = "connectivity.previously_offline"
IP_PING_NAMESPACE = "device_checks.ip_ping_failures"
IP_PING_CHECK_ID = "ip_ping"
LOOP_NAME = "device_connectivity"
_MACHINE = DeviceStateMachine(hooks=[EventLogHook(), IncidentHook(), RunExclusionHook()])


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


async def _get_device_health(
    device: Device,
    *,
    ip_ping_timeout_sec: float | None = None,
    ip_ping_count: int | None = None,
) -> dict[str, Any] | None:
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
            ip_ping_timeout_sec=ip_ping_timeout_sec,
            ip_ping_count=ip_ping_count,
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


def _split_ip_ping(checks: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Separate the ip_ping check entry from the remaining checks list."""
    ip_ping: dict[str, Any] | None = None
    others: list[dict[str, Any]] = []
    for entry in checks:
        if isinstance(entry, dict) and entry.get("check_id") == IP_PING_CHECK_ID:
            ip_ping = entry
        else:
            others.append(entry)
    return ip_ping, others


async def _apply_ip_ping_hysteresis(
    db: AsyncSession,
    device: Device,
    *,
    ok: bool,
    threshold: int,
) -> bool:
    """Increment / reset the consecutive-failure counter and return the gated boolean.

    Returns True while the failure count is below threshold (suppressing the
    failure), False once the count reaches or exceeds threshold, and always
    True (plus counter reset) on success.
    """
    if ok:
        await control_plane_state_store.delete_value(db, IP_PING_NAMESPACE, device.identity_value)
        return True

    current = await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
    counter = int(current) + 1 if isinstance(current, int) else 1
    await control_plane_state_store.set_value(db, IP_PING_NAMESPACE, device.identity_value, counter)
    return counter < threshold


async def _stop_disconnected_node(db: AsyncSession, device: Device) -> bool | None:
    locked_device = await device_locking.lock_device(db, device.id)
    locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)
    if locked_node is None or locked_node.state == NodeState.stopped:
        return None

    await write_desired_state(
        db,
        node=locked_node,
        target=NodeState.stopped,
        caller="connectivity",
    )
    await device_health.apply_node_state_transition(db, locked_device, new_state=locked_node.state, mark_offline=True)
    return None


async def reset_connectivity_control_plane_state(db: AsyncSession) -> None:
    await control_plane_state_store.delete_namespace(db, CONNECTIVITY_NAMESPACE)
    await db.commit()


async def get_connectivity_control_plane_state(db: AsyncSession) -> set[str]:
    return set((await control_plane_state_store.get_values(db, CONNECTIVITY_NAMESPACE)).keys())


async def track_previously_offline_device(db: AsyncSession, identity_value: str) -> None:
    await control_plane_state_store.set_value(db, CONNECTIVITY_NAMESPACE, identity_value, True)
    await db.commit()


async def _check_connectivity(db: AsyncSession) -> None:
    ip_ping_threshold = int(settings_service.get("device_checks.ip_ping.consecutive_fail_threshold"))
    ip_ping_timeout = float(settings_service.get("device_checks.ip_ping.timeout_sec"))
    ip_ping_count = int(settings_service.get("device_checks.ip_ping.count_per_cycle"))

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

        await assert_current_leader(db)

        for device in devices:
            lifecycle_state = await _get_lifecycle_state(db, device)
            await assert_current_leader(db)
            if lifecycle_state is not None:
                await device_health.update_emulator_state(db, device, lifecycle_state)

            if _device_expected_aliases(device) & connected_targets:
                # Device is connected
                health_result = await _get_device_health(
                    device,
                    ip_ping_timeout_sec=ip_ping_timeout,
                    ip_ping_count=ip_ping_count,
                )
                await assert_current_leader(db)
                if health_result is not None:
                    raw_checks = health_result.get("checks") or []
                    raw_checks_list = list(raw_checks) if isinstance(raw_checks, list) else []
                    ip_ping_entry, other_checks = _split_ip_ping(raw_checks_list)

                    # When no checks are listed at all, trust the top-level healthy flag.
                    # When checks are listed, derive health from individual check results.
                    if not raw_checks_list:
                        others_ok = bool(health_result.get("healthy", True))
                    else:
                        others_ok = all(bool(c.get("ok")) for c in other_checks if isinstance(c, dict))
                    gated_ip_ping_ok = True
                    if ip_ping_entry is not None and device.hold != DeviceHold.maintenance and device.auto_manage:
                        gated_ip_ping_ok = await _apply_ip_ping_hysteresis(
                            db,
                            device,
                            ok=bool(ip_ping_entry.get("ok")),
                            threshold=ip_ping_threshold,
                        )
                        if not bool(ip_ping_entry.get("ok")):
                            metrics.record_ip_ping_failure(device_identity=device.identity_value, host=host.hostname)
                        counter_value = await control_plane_state_store.get_value(
                            db, IP_PING_NAMESPACE, device.identity_value
                        )
                        metrics.set_ip_ping_consecutive_failures(
                            device_identity=device.identity_value,
                            host=host.hostname,
                            value=int(counter_value or 0),
                        )
                    healthy = others_ok and gated_ip_ping_ok

                    if not healthy:
                        summary = _summarize_unhealthy_result(health_result)
                        await device_health.update_device_checks(
                            db,
                            device,
                            healthy=False,
                            summary=summary,
                        )
                        await lifecycle_policy.handle_health_failure(
                            db,
                            device,
                            source="device_checks",
                            reason=summary,
                        )
                        await control_plane_state_store.set_value(
                            db, CONNECTIVITY_NAMESPACE, device.identity_value, True
                        )
                        continue
                    else:
                        counter = (
                            await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
                            if ip_ping_entry is not None
                            else None
                        )
                        summary = (
                            f"Healthy (ip_ping miss {counter}/{ip_ping_threshold})"
                            if isinstance(counter, int) and counter > 0
                            else "Healthy"
                        )
                        await device_health.update_device_checks(
                            db,
                            device,
                            healthy=True,
                            summary=summary,
                        )

                if device.operational_state == DeviceOperationalState.offline:
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
                    health_result = await _get_device_health(
                        device,
                        ip_ping_timeout_sec=ip_ping_timeout,
                        ip_ping_count=ip_ping_count,
                    )
                    await assert_current_leader(db)
                    if health_result is not None:
                        raw_checks = health_result.get("checks") or []
                        raw_checks_list = list(raw_checks) if isinstance(raw_checks, list) else []
                        ip_ping_entry, other_checks = _split_ip_ping(raw_checks_list)

                        # When no checks are listed at all, trust the top-level healthy flag.
                        # When checks are listed, derive health from individual check results.
                        if not raw_checks_list:
                            others_ok = bool(health_result.get("healthy", True))
                        else:
                            others_ok = all(bool(c.get("ok")) for c in other_checks if isinstance(c, dict))
                        gated_ip_ping_ok = True
                        if ip_ping_entry is not None and device.hold != DeviceHold.maintenance and device.auto_manage:
                            gated_ip_ping_ok = await _apply_ip_ping_hysteresis(
                                db,
                                device,
                                ok=bool(ip_ping_entry.get("ok")),
                                threshold=ip_ping_threshold,
                            )
                            if not bool(ip_ping_entry.get("ok")):
                                metrics.record_ip_ping_failure(
                                    device_identity=device.identity_value, host=host.hostname
                                )
                            counter_value = await control_plane_state_store.get_value(
                                db, IP_PING_NAMESPACE, device.identity_value
                            )
                            metrics.set_ip_ping_consecutive_failures(
                                device_identity=device.identity_value,
                                host=host.hostname,
                                value=int(counter_value or 0),
                            )
                        healthy = others_ok and gated_ip_ping_ok

                        if healthy:
                            counter = (
                                await control_plane_state_store.get_value(db, IP_PING_NAMESPACE, device.identity_value)
                                if ip_ping_entry is not None
                                else None
                            )
                            summary = (
                                f"Healthy (ip_ping miss {counter}/{ip_ping_threshold})"
                                if isinstance(counter, int) and counter > 0
                                else "Healthy"
                            )
                            await device_health.update_device_checks(db, device, healthy=True, summary=summary)
                            if device.operational_state == DeviceOperationalState.offline:
                                if not await is_ready_for_use_async(db, device):
                                    logger.debug(
                                        "Device %s is healthy but still awaiting setup/verification", device.name
                                    )
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
                # Maintenance devices are placed there by operators; transient
                # disconnects are not actionable — skip silently to match pre-PR
                # behavior (no connectivity_lost event, no lifecycle write).
                if device.hold == DeviceHold.maintenance:
                    continue
                if not device.auto_manage:
                    continue
                await assert_current_leader(db)
                stopped_node = await _stop_disconnected_node(db, device)
                if device.operational_state == DeviceOperationalState.offline:
                    if stopped_node is not None:
                        await control_plane_state_store.set_value(
                            db,
                            CONNECTIVITY_NAMESPACE,
                            device.identity_value,
                            True,
                        )
                    continue
                if device.operational_state == DeviceOperationalState.busy or device.hold is not None:
                    logger.warning(
                        "Device %s (%s) appears disconnected on host %s but is %s",
                        device.name,
                        device.identity_value,
                        host.hostname,
                        legacy_label_for_audit(device),
                    )
                    await device_health.update_device_checks(db, device, healthy=False, summary="Disconnected")
                    locked_device = await device_locking.lock_device(db, device.id)
                    if locked_device.operational_state == DeviceOperationalState.busy or locked_device.hold is not None:
                        await _MACHINE.transition(
                            locked_device,
                            TransitionEvent.CONNECTIVITY_LOST,
                            reason="Device disconnected",
                        )
                        await lifecycle_policy.note_connectivity_loss(db, locked_device, reason="Device disconnected")
                        await control_plane_state_store.set_value(
                            db, CONNECTIVITY_NAMESPACE, locked_device.identity_value, True
                        )
                    else:
                        logger.info(
                            "Device %s (%s) left held/busy state before lifecycle write — skipping",
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
                await device_health.update_device_checks(db, device, healthy=False, summary="Disconnected")
                locked_device = await device_locking.lock_device(db, device.id)
                if locked_device.operational_state != DeviceOperationalState.busy and locked_device.hold is None:
                    await _MACHINE.transition(
                        locked_device,
                        TransitionEvent.CONNECTIVITY_LOST,
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
                        legacy_label_for_audit(locked_device),
                    )

    await db.commit()


async def device_connectivity_loop() -> None:
    """Background loop that checks device connectivity via host agents."""
    while True:
        interval = float(settings_service.get("general.device_check_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                await _check_connectivity(db)
        except LeadershipLost as exc:
            logger.error(
                "device_connectivity_loop_leadership_lost",
                reason=str(exc),
                action="exiting_process_to_prevent_split_brain",
            )
            os._exit(70)
        except Exception:
            logger.exception("Device connectivity check failed")
        await asyncio.sleep(interval)
