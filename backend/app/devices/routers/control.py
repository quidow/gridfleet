import uuid
from typing import Annotated, Any

import httpx2 as httpx
from fastapi import APIRouter, HTTPException, Query

from app.agent_comm.dependencies import AgentCommServicesDep
from app.agent_comm.operations import appium_logs, pack_device_lifecycle_action
from app.agent_comm.operations import appium_status as fetch_appium_status
from app.agent_comm.operations import pack_device_health as fetch_pack_device_health
from app.appium_nodes.dependencies import AppiumNodeServicesDep
from app.appium_nodes.models import AppiumDesiredState
from app.appium_nodes.services import reconciler_agent as node_manager
from app.core.dependencies import DbDep
from app.core.error_responses import RESPONSES_400, RESPONSES_401, RESPONSES_404, RESPONSES_409
from app.core.errors import AgentCallError
from app.devices.dependencies import DeviceServicesDep
from app.devices.routers.helpers import (
    get_device_for_update_or_404,
    get_device_or_404,
)
from app.devices.schemas.device import (
    ConfigAuditEntryRead,
    DeviceConfigRead,
    DeviceHealthRead,
    DeviceRead,
    SessionViabilityRead,
)
from app.devices.schemas.maintenance import DeviceMaintenanceUpdate
from app.devices.services import identity, lifecycle_policy_summary, link_repair
from app.devices.services import intent as intent_service
from app.packs.services import platform_catalog as pack_platform_catalog
from app.packs.services import platform_resolver as pack_platform_resolver
from app.sessions.dependencies import SessionServicesDep
from app.sessions.viability_types import SessionViabilityCheckedBy
from app.settings import service_config as config_service
from app.settings.dependencies import SettingsServicesDep

appium_connection_target = identity.appium_connection_target
platform_has_lifecycle_action = pack_platform_catalog.platform_has_lifecycle_action
require_management_host = node_manager.require_management_host
resolve_pack_platform = pack_platform_resolver.resolve_pack_platform

DEVICE_CONTROL_ERROR_RESPONSES = {**RESPONSES_400, **RESPONSES_401, **RESPONSES_404, **RESPONSES_409}

router = APIRouter(responses=DEVICE_CONTROL_ERROR_RESPONSES)


@router.post("/{device_id}/maintenance", response_model=DeviceRead)
async def enter_device_maintenance(
    device_id: uuid.UUID,
    body: DeviceMaintenanceUpdate,
    db: DbDep,
    device_services: DeviceServicesDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        device = await device_services.maintenance.enter_maintenance(db, device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return await device_services.presenter.serialize_device(db, device)


@router.post("/{device_id}/maintenance/exit", response_model=DeviceRead)
async def exit_device_maintenance(
    device_id: uuid.UUID, db: DbDep, device_services: DeviceServicesDep
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        device = await device_services.maintenance.exit_maintenance(db, device)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return await device_services.presenter.serialize_device(db, device)


@router.get("/{device_id}/config", response_model=DeviceConfigRead)
async def get_device_config(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    keys: Annotated[str | None, Query(description="Comma-separated list of keys to return")] = None,
) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db, device_services.crud)
    key_list = [k.strip() for k in keys.split(",")] if keys else None
    return await config_service.get_device_config(db, device, keys=key_list)


@router.patch("/{device_id}/config", response_model=DeviceConfigRead)
async def merge_device_config(
    device_id: uuid.UUID,
    body: dict[str, Any],
    db: DbDep,
    settings_services: SettingsServicesDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    return await settings_services.config.merge_device_config(db, device, body)


@router.get("/{device_id}/config/history", response_model=list[ConfigAuditEntryRead])
async def get_config_history(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    settings_services: SettingsServicesDep,
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[dict[str, Any]]:
    await get_device_or_404(device_id, db, device_services.crud)
    logs = await settings_services.config.get_config_history(db, device_id, limit=limit)
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
async def device_health(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    session_services: SessionServicesDep,
) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db, device_services.crud)
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
                settings=settings_services.service,
                circuit_breaker=agent_comm.circuit_breaker,
                pool=agent_comm.http_pool,
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
            settings=settings_services.service,
            circuit_breaker=agent_comm.circuit_breaker,
            pool=agent_comm.http_pool,
        )
    except AgentCallError as e:
        result["device_checks"] = {"healthy": False, "detail": f"Agent unreachable: {e}"}

    result["session_viability"] = await session_services.viability.get_session_viability(db, device)
    session_viability_failed = (
        result["session_viability"] is not None and result["session_viability"].get("status") == "failed"
    )
    result["lifecycle_policy"] = await lifecycle_policy_summary.build_lifecycle_policy(db, device)
    result["healthy"] = (
        result["device_checks"].get("healthy", False)
        and not session_viability_failed
        and (node is None or result["node"]["running"])
    )

    return result


@router.post("/{device_id}/session-test", response_model=SessionViabilityRead)
async def device_session_test(
    device_id: uuid.UUID,
    db: DbDep,
    session_services: SessionServicesDep,
) -> dict[str, Any]:
    device = await get_device_for_update_or_404(device_id, db)
    try:
        return await session_services.viability.run_session_viability_probe(
            db,
            device,
            checked_by=SessionViabilityCheckedBy.manual,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{device_id}/reconnect")
async def reconnect_device(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    appium_services: AppiumNodeServicesDep,
) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db, device_services.crud)

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

    data = await link_repair.dispatch_recommended_action(
        device,
        "reconnect",
        settings=settings_services.service,
        circuit_breaker=agent_comm.circuit_breaker,
        pool=agent_comm.http_pool,
    )

    success = data.get("success", False)

    if success and device.appium_node:
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
            await db.flush()
            await intent_service.IntentService(db).revoke_intents_and_reconcile(
                device_id=device.id,
                sources=[
                    f"connectivity:{device.id}",
                    f"health_failure:node:{device.id}",
                    f"health_failure:recovery:{device.id}",
                ],
                reason="Operator reconnect succeeded",
                publisher=device_services.publisher,
            )
            # Intent reconciliation briefly locks the device row. Commit before
            # the inline restart so maintenance/delete actions can still
            # preempt while the restart talks to the agent.
            await db.commit()
            node = device.appium_node
            if node is None or not node.observed_running:
                if device.host_id is None:
                    raise HTTPException(status_code=400, detail=f"Device {device.id} has no host assigned")
                await appium_services.reconciler_agent.start_node(db, device, caller="operator_route")
            else:
                await appium_services.reconciler_agent.restart_node(db, device, caller="operator_restart")
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
    device_services: DeviceServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
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
        settings=settings_services.service,
        circuit_breaker=agent_comm.circuit_breaker,
        pool=agent_comm.http_pool,
    )
    if action == "state" and isinstance(result.get("state"), str):
        await device_services.health.update_emulator_state(db, device, result["state"])
        await db.commit()
    return result


@router.get("/{device_id}/logs")
async def device_logs(
    device_id: uuid.UUID,
    db: DbDep,
    device_services: DeviceServicesDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
    lines: Annotated[int, Query(le=5000)] = 100,
) -> dict[str, Any]:
    device = await get_device_or_404(device_id, db, device_services.crud)
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
            settings=settings_services.service,
            circuit_breaker=agent_comm.circuit_breaker,
            pool=agent_comm.http_pool,
        )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Cannot fetch logs from agent: {e}") from e
