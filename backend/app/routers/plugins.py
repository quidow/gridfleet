import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.appium_plugin import AppiumPlugin
from app.schemas.plugin import (
    FleetPluginSyncResult,
    HostPluginStatus,
    PluginCreate,
    PluginRead,
    PluginSyncResult,
    PluginUpdate,
)
from app.services import host_service, plugin_service

router = APIRouter(prefix="/api", tags=["plugins"])


@router.get("/plugins", response_model=list[PluginRead])
async def list_plugins(db: AsyncSession = Depends(get_db)) -> list[AppiumPlugin]:
    return await plugin_service.list_plugins(db)


@router.post("/plugins", response_model=PluginRead, status_code=201)
async def create_plugin(data: PluginCreate, db: AsyncSession = Depends(get_db)) -> AppiumPlugin:
    try:
        return await plugin_service.create_plugin(db, data)
    except IntegrityError:
        raise HTTPException(status_code=409, detail="Plugin with this name already exists") from None


@router.patch("/plugins/{plugin_id}", response_model=PluginRead)
async def update_plugin(plugin_id: uuid.UUID, data: PluginUpdate, db: AsyncSession = Depends(get_db)) -> AppiumPlugin:
    plugin = await plugin_service.update_plugin(db, plugin_id, data)
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return plugin


@router.delete("/plugins/{plugin_id}", status_code=204)
async def delete_plugin(plugin_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> None:
    deleted = await plugin_service.delete_plugin(db, plugin_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Plugin not found")


@router.post("/plugins/sync-all", response_model=FleetPluginSyncResult)
async def sync_all_plugins(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    return await plugin_service.sync_all_host_plugins(db)


@router.get("/hosts/{host_id}/plugins", response_model=list[HostPluginStatus])
async def host_plugins(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    all_plugins = await plugin_service.list_plugins(db)
    return await plugin_service.get_host_plugin_statuses(host, all_plugins)


@router.post("/hosts/{host_id}/plugins/sync", response_model=PluginSyncResult)
async def sync_host_plugins(host_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    host = await host_service.get_host(db, host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="Host not found")
    all_plugins = await plugin_service.list_plugins(db)
    return await plugin_service.sync_host_plugins(host, all_plugins)
