import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError

from app.agent_comm.dependencies import AgentCommServicesDep
from app.core.dependencies import DbDep
from app.hosts import service as host_service
from app.plugins.models import AppiumPlugin
from app.plugins.schemas import (
    FleetPluginSyncResult,
    HostPluginStatus,
    PluginCreate,
    PluginRead,
    PluginSyncResult,
    PluginUpdate,
)
from app.plugins.service import PluginService
from app.settings.dependencies import SettingsServicesDep

router = APIRouter(prefix="/api", tags=["plugins"])


@router.get("/plugins", response_model=list[PluginRead])
async def list_plugins(
    db: DbDep, settings_services: SettingsServicesDep, agent_comm: AgentCommServicesDep
) -> list[AppiumPlugin]:
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    return await svc.list_plugins(db)


@router.post("/plugins", response_model=PluginRead, status_code=201)
async def create_plugin(
    data: PluginCreate, db: DbDep, settings_services: SettingsServicesDep, agent_comm: AgentCommServicesDep
) -> AppiumPlugin:
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    try:
        return await svc.create_plugin(db, data)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Plugin with this name already exists") from None


@router.patch("/plugins/{plugin_id}", response_model=PluginRead)
async def update_plugin(
    plugin_id: uuid.UUID,
    data: PluginUpdate,
    db: DbDep,
    settings_services: SettingsServicesDep,
    agent_comm: AgentCommServicesDep,
) -> AppiumPlugin:
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    plugin = await svc.update_plugin(db, plugin_id, data)
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return plugin


@router.delete("/plugins/{plugin_id}", status_code=204)
async def delete_plugin(
    plugin_id: uuid.UUID, db: DbDep, settings_services: SettingsServicesDep, agent_comm: AgentCommServicesDep
) -> None:
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    deleted = await svc.delete_plugin(db, plugin_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Plugin not found")


@router.post("/plugins/sync-all", response_model=FleetPluginSyncResult)
async def sync_all_plugins(
    db: DbDep, settings_services: SettingsServicesDep, agent_comm: AgentCommServicesDep
) -> dict[str, Any]:
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    return await svc.sync_all_host_plugins(db)


@router.get("/hosts/{host_id}/plugins", response_model=list[HostPluginStatus])
async def host_plugins(
    host_id: uuid.UUID, db: DbDep, settings_services: SettingsServicesDep, agent_comm: AgentCommServicesDep
) -> list[dict[str, Any]]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    all_plugins = await svc.list_plugins(db)
    return await svc.get_host_plugin_statuses(host, all_plugins)


@router.post("/hosts/{host_id}/plugins/sync", response_model=PluginSyncResult)
async def sync_host_plugins(
    host_id: uuid.UUID, db: DbDep, settings_services: SettingsServicesDep, agent_comm: AgentCommServicesDep
) -> dict[str, Any]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    svc = PluginService(settings=settings_services.service, circuit_breaker=agent_comm.circuit_breaker)
    all_plugins = await svc.list_plugins(db)
    return await svc.sync_host_plugins(host, all_plugins)
