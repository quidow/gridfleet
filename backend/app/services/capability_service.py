import logging
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceType
from app.services import (
    appium_capability_keys,
    appium_node_locking,
    appium_node_resource_service,
    control_plane_state_store,
    device_locking,
)
from app.services.device_identity import appium_connection_target
from app.services.host_diagnostics import APPIUM_PROCESSES_NAMESPACE
from app.services.pack_capability_service import (
    render_default_capabilities,
    render_device_field_capabilities,
    render_stereotype,
)
from app.services.pack_platform_resolver import resolve_pack_platform
from app.services.pack_start_shim import resolve_pack_for_device

logger = logging.getLogger(__name__)


def _is_running_emulator(device: Device) -> bool:
    """Check whether the device is a running emulator with an active Appium node."""
    node = device.appium_node
    return node is not None and node.pid is not None and device.device_type == DeviceType.emulator


def _appium_udid_for_capabilities(device: Device, active_connection_target: str | None = None) -> str:
    if device.device_type == DeviceType.emulator and active_connection_target:
        return active_connection_target
    return appium_connection_target(device)


def build_capabilities(
    device: Device,
    automation_name: str | None,
    *,
    appium_platform_name: str | None = None,
    session_caps: dict[str, Any] | None = None,
    active_connection_target: str | None = None,
) -> dict[str, Any]:
    """Build the minimal Appium capabilities dict for *device*.

    *appium_platform_name* is resolved from the pack manifest by the async
    ``get_device_capabilities`` path.
    """
    caps: dict[str, Any] = {}
    if session_caps:
        caps.update(session_caps)

    if appium_platform_name is None:
        raise LookupError(f"Device {device.id} has no resolved Appium platform name")
    caps["platformName"] = appium_platform_name
    caps["appium:udid"] = _appium_udid_for_capabilities(device, active_connection_target)
    caps["appium:deviceName"] = device.name
    if device.id:
        caps["appium:gridfleet:deviceId"] = str(device.id)

    if automation_name:
        caps["appium:automationName"] = automation_name

    if str(device.device_type) == "simulator":
        caps["appium:simulatorRunning"] = True

    return caps


async def _active_target_from_host_snapshot(db: AsyncSession, device: Device) -> str | None:
    node = device.appium_node
    if node is None or device.host_id is None:
        return None
    snapshot = await control_plane_state_store.get_value(db, APPIUM_PROCESSES_NAMESPACE, str(device.host_id))
    if not isinstance(snapshot, dict):
        return None
    running_nodes = snapshot.get("running_nodes")
    if not isinstance(running_nodes, list):
        return None
    for raw_node in running_nodes:
        if not isinstance(raw_node, dict) or raw_node.get("port") != node.port:
            continue
        connection_target = raw_node.get("connection_target")
        if isinstance(connection_target, str) and connection_target:
            return connection_target
    return None


async def _get_live_active_connection_target(db: AsyncSession, device: Device) -> str | None:
    if not _is_running_emulator(device):
        return None
    node = device.appium_node
    if node is not None and node.active_connection_target:
        return cast("str", node.active_connection_target)

    # The node exists but active_connection_target is not yet cached — we must
    # fetch it from the host snapshot and persist it.  Acquire Device + AppiumNode
    # row locks BEFORE the slow host-snapshot call so that concurrent writers
    # (mark_node_started / mark_node_stopped) cannot race the same column.
    await device_locking.lock_device(db, device.id)
    locked_node = await appium_node_locking.lock_appium_node_for_device(db, device.id)

    active_connection_target = await _active_target_from_host_snapshot(db, device)
    if locked_node is not None and active_connection_target:
        locked_node.active_connection_target = active_connection_target
        await db.flush()
    return active_connection_target


async def get_device_capabilities(
    db: AsyncSession,
    device: Device,
    *,
    active_connection_target: str | None = None,
) -> dict[str, Any]:
    """Fetch the automation name from the pack catalog and build capabilities for *device*."""
    automation_name: str | None = None
    appium_platform_name: str | None = None
    pack_caps: dict[str, Any] = {}
    manager_owned = appium_capability_keys.core_manager_owned_cap_keys()
    resolved = resolve_pack_for_device(device)
    if resolved is not None:
        pack_id, platform_id = resolved
        try:
            stereotype = await render_stereotype(db, pack_id=pack_id, platform_id=platform_id)
            automation_name = stereotype.get("appium:automationName")
            appium_platform_name = stereotype.get("platformName")
        except LookupError:
            logger.debug("Stereotype not found for pack=%s platform=%s", pack_id, platform_id, exc_info=True)
        try:
            resolved_plat = await resolve_pack_platform(
                db,
                pack_id=pack_id,
                platform_id=platform_id,
                device_type=device.device_type.value if device.device_type else None,
            )
            manager_owned = appium_capability_keys.manager_owned_cap_keys(
                frozenset(port.capability_name for port in resolved_plat.parallel_resources.ports)
            )
            if appium_platform_name is None:
                appium_platform_name = resolved_plat.appium_platform_name
            device_context = {
                "ip_address": device.ip_address,
                "connection_target": getattr(device, "connection_target", None),
                "identity_value": getattr(device, "identity_value", None),
                "os_version": device.os_version,
            }
            pack_caps.update(render_default_capabilities(resolved_plat, device_context=device_context))
            pack_caps.update(render_device_field_capabilities(resolved_plat, device.device_config or {}))
        except LookupError:
            raise
    user_caps = appium_capability_keys.sanitize_appium_caps(
        (device.device_config or {}).get("appium_caps"),
        manager_owned=manager_owned,
    )
    if device.appium_node is None or not device.appium_node.observed_running:
        live_caps = {}
    else:
        live_caps = await appium_node_resource_service.get_capabilities(db, node_id=device.appium_node.id)
    if active_connection_target is None:
        active_connection_target = await _get_live_active_connection_target(db, device)
    overlay = {**pack_caps, **user_caps, **live_caps}
    return build_capabilities(
        device,
        automation_name,
        appium_platform_name=appium_platform_name,
        session_caps=overlay,
        active_connection_target=active_connection_target,
    )
