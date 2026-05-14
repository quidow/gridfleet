import uuid
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query

from app.dependencies import DbDep
from app.errors import AgentCallError
from app.models.appium_node import AppiumDesiredState
from app.routers.device_route_helpers import (
    get_device_for_update_or_404,
    get_device_or_404,
)
from app.schemas.device import (
    ConfigAuditEntryRead,
    DeviceConfigRead,
    DeviceHealthRead,
    DeviceRead,
    SessionViabilityRead,
)
from app.schemas.maintenance import DeviceMaintenanceUpdate
from app.services import appium_reconciler_agent as node_manager
from app.services import (
    config_service,
    device_presenter,
    lifecycle_policy,
    maintenance_service,
    pack_discovery_service,
    session_viability,
)
from app.services import (
    device_health as device_health_service,
)
from app.services.agent_operations import (
    appium_logs,
    get_pack_device_properties,
    pack_device_lifecycle_action,
)
from app.services.agent_operations import (
    appium_status as fetch_appium_status,
)
from app.services.agent_operations import (
    pack_device_health as fetch_pack_device_health,
)
from app.services.appium_reconciler_agent import require_management_host
from app.services.device_identity import appium_connection_target
from app.services.intent_service import revoke_intents_and_reconcile
from app.services.pack_platform_catalog import platform_has_lifecycle_action
from app.services.pack_platform_resolver import resolve_pack_platform
from app.services.session_viability_types import SessionViabilityCheckedBy

router = APIRouter()


@router.post("/{device_id}/maintenance", response_model=DeviceRead)
async def enter_device_maintenance(
    device_id: uuid.UUID,
    body: DeviceMaintenanceUpdate,
    db: DbDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        device = await maintenance_service.enter_maintenance(db, device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return await device_presenter.serialize_device(db, device)


@router.post("/{device_id}/maintenance/exit", response_model=DeviceRead)
async def exit_device_maintenance(device_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        device = await maintenance_service.exit_maintenance(db, device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return await device_presenter.serialize_device(db, device)


@router.get("/{device_id}/config", response_model=DeviceConfigRead)
async def get_device_config(
    device_id: uuid.UUID,
    db: DbDep,
    keys: str | None = Query(None, description="Comma-separated list of keys to return"),
) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)
    key_list = [k.strip() for k in keys.split(",")] if keys else None
    return await config_service.get_device_config(db, device, keys=key_list)


@router.put("/{device_id}/config", response_model=DeviceConfigRead)
async def replace_device_config(
    device_id: uuid.UUID,
    body: dict[str, Any],
    db: DbDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await config_service.replace_device_config(db, device, body)


@router.patch("/{device_id}/config", response_model=DeviceConfigRead)
async def merge_device_config(
    device_id: uuid.UUID,
    body: dict[str, Any],
    db: DbDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await config_service.merge_device_config(db, device, body)


@router.get("/{device_id}/config/history", response_model=list[ConfigAuditEntryRead])
async def get_config_history(
    device_id: uuid.UUID,
    db: DbDep,
    limit: int = Query(50, le=200),
) -> list[dict[str, Any]]:
    await get_device_or_404(device_id, db)
    logs = await config_service.get_config_history(db, device_id, limit=limit)
    return [
        {
            "id": str(log.id),
            "previous_config": log.previous_config,
            "new_config": log.new_config,
            "changed_by": log.changed_by,
            "changed_at": log.changed_at.isoformat(),
        }
        for log in logs
    ]


@router.get("/{device_id}/health", response_model=DeviceHealthRead)
async def device_health(device_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)
    host = require_management_host(device, action="inspect device health")

    result: dict[str, Any] = {"platform": device.platform_id}

    node = device.appium_node
    node_lifecycle_state = "running" if node is not None and node.observed_running else ("stopped" if node else None)
    node_health_running = getattr(node, "health_running", None) if node is not None else None
    node_health_state = getattr(node, "health_state", None) if node is not None else None
    node_running = node is not None and (
        node_health_running
        if node_health_running is not None
        else node_lifecycle_state == AppiumDesiredState.running.value
    )
    node_state = node_health_state if node_health_state is not None else node_lifecycle_state
    if node is not None:
        try:
            node_payload = await fetch_appium_status(
                host.ip,
                host.agent_port,
                node.port,
                http_client_factory=httpx.AsyncClient,
            )
            node_running = node_payload is not None and node_payload.get("running", False) is True
            if not node_running and node_state == "running":
                node_state = "error"
        except AgentCallError:
            node_running = False
            if node_state == "running":
                node_state = "error"

    result["node"] = {
        "running": node_running,
        "port": node.port if node else None,
        "state": node_state,
    }

    connection_target = appium_connection_target(device)
    try:
        result["device_checks"] = await fetch_pack_device_health(
            host.ip,
            host.agent_port,
            connection_target,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else "real_device",
            connection_type=device.connection_type.value if device.connection_type else None,
            ip_address=device.ip_address,
            http_client_factory=httpx.AsyncClient,
        )
    except AgentCallError as e:
        result["device_checks"] = {"healthy": False, "detail": f"Agent unreachable: {e}"}

    result["session_viability"] = await session_viability.get_session_viability(db, device)
    session_viability_failed = (
        result["session_viability"] is not None and result["session_viability"].get("status") == "failed"
    )
    result["lifecycle_policy"] = await lifecycle_policy.build_lifecycle_policy(db, device)
    result["healthy"] = (
        result["device_checks"].get("healthy", False)
        and not session_viability_failed
        and (node is None or result["node"]["running"])
    )

    return result


@router.post("/{device_id}/session-test", response_model=SessionViabilityRead)
async def device_session_test(device_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        return await session_viability.run_session_viability_probe(
            db, device, checked_by=SessionViabilityCheckedBy.manual
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{device_id}/reconnect")
async def reconnect_device(device_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)

    try:
        resolved = await resolve_pack_platform(
            db,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail="Device pack/platform not found in catalog") from exc
    if not platform_has_lifecycle_action(resolved.lifecycle_actions, "reconnect"):
        raise HTTPException(status_code=400, detail="Reconnect is not supported for this device platform")
    if device.connection_type.value != "network":
        raise HTTPException(status_code=400, detail="Reconnect is only supported for network-connected devices")
    if not device.ip_address:
        raise HTTPException(status_code=400, detail="Device has no IP address")
    if not device.host:
        raise HTTPException(status_code=400, detail="Device has no host assigned")
    if not device.connection_target:
        raise HTTPException(status_code=400, detail="Device has no connection target")

    host = device.host
    data = await pack_device_lifecycle_action(
        host.ip,
        host.agent_port,
        device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        action="reconnect",
        args={"ip_address": device.ip_address, "port": 5555},
        http_client_factory=httpx.AsyncClient,
    )

    success = data.get("success", False)

    if success and device.auto_manage and device.appium_node:
        # Intentionally NOT re-fetched with `get_device_for_update_or_404` here:
        # Inline restart writes desired state directly so concurrent operator
        # actions (maintenance enter, delete) can preempt — see
        # `tests/test_concurrency_reconnect_restart_lock.py`. Locking at the
        # router would serialise these and break preemption.
        #
        # Clear stale session-viability failures so health checks can evaluate
        # the device after the node restarts.
        device.session_viability_status = None
        device.session_viability_error = None
        try:
            await revoke_intents_and_reconcile(
                db,
                device_id=device.id,
                sources=[
                    f"connectivity:{device.id}",
                    f"health_failure:node:{device.id}",
                    f"health_failure:recovery:{device.id}",
                ],
                reason="Operator reconnect succeeded",
            )
            # Intent reconciliation briefly locks the device row. Commit before
            # the inline restart so maintenance/delete actions can still
            # preempt while the restart talks to the agent.
            await db.commit()
            node = device.appium_node
            if node is None or not node.observed_running:
                if device.host_id is None:
                    raise HTTPException(status_code=400, detail=f"Device {device.id} has no host assigned")
                await node_manager.start_node(db, device, caller="operator_route")
            else:
                await node_manager.restart_node(db, device, caller="operator_restart")
        except (node_manager.NodeManagerError, node_manager.NodePortConflictError) as exc:
            raise HTTPException(status_code=502, detail=f"Reconnect succeeded but node restart failed: {exc}") from exc

    return {
        "success": success,
        "identity_value": device.identity_value,
        "message": "Reconnected" if success else "Reconnect failed",
    }


@router.post("/{device_id}/lifecycle/{action}")
async def device_lifecycle_action(
    device_id: uuid.UUID,
    action: str,
    db: DbDep,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        resolved = await resolve_pack_platform(
            db,
            pack_id=device.pack_id,
            platform_id=device.platform_id,
            device_type=device.device_type.value if device.device_type else None,
        )
    except LookupError as exc:
        raise HTTPException(status_code=400, detail="Device pack/platform not found in catalog") from exc
    if not platform_has_lifecycle_action(resolved.lifecycle_actions, action):
        raise HTTPException(
            status_code=400,
            detail=f"Lifecycle action {action} is not supported for this device platform",
        )
    if not device.host:
        raise HTTPException(status_code=400, detail="Device has no host assigned")
    if not device.connection_target:
        raise HTTPException(status_code=400, detail="Device has no connection target")

    result = await pack_device_lifecycle_action(
        device.host.ip,
        device.host.agent_port,
        device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        action=action,
        args=body or {},
        http_client_factory=httpx.AsyncClient,
    )
    if action == "state" and isinstance(result.get("state"), str):
        await device_health_service.update_emulator_state(db, device, result["state"])
        await db.commit()
    return result


@router.post("/{device_id}/refresh", response_model=DeviceRead)
async def refresh_device_properties(device_id: uuid.UUID, db: DbDep) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)
    if not device.host_id:
        raise HTTPException(status_code=400, detail="Device has no host — cannot refresh properties")
    await pack_discovery_service.refresh_device_properties(
        db,
        device,
        agent_get_pack_device_properties=get_pack_device_properties,
    )
    await db.refresh(device)
    return await device_presenter.serialize_device(db, device)


@router.get("/{device_id}/logs")
async def device_logs(
    device_id: uuid.UUID,
    db: DbDep,
    lines: int = Query(100, le=5000),
) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db)
    host = require_management_host(device, action="fetch device logs")

    node = device.appium_node
    if not node:
        return {"lines": [], "count": 0}

    try:
        return await appium_logs(
            host.ip,
            host.agent_port,
            node.port,
            lines=lines,
            http_client_factory=httpx.AsyncClient,
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Cannot fetch logs from agent: {e}") from e
