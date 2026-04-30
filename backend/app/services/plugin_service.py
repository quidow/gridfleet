import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import AgentCallError
from app.models.appium_plugin import AppiumPlugin
from app.models.host import Host, HostStatus
from app.observability import get_logger
from app.schemas.plugin import PluginCreate, PluginUpdate
from app.services.agent_operations import list_plugins as list_agent_plugins
from app.services.agent_operations import sync_plugins as sync_agent_plugins

logger = get_logger(__name__)


def _filter_enabled(plugins: list[AppiumPlugin]) -> list[AppiumPlugin]:
    return [p for p in plugins if p.enabled]


async def list_plugins(db: AsyncSession) -> list[AppiumPlugin]:
    stmt = select(AppiumPlugin).order_by(AppiumPlugin.name)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_plugin(db: AsyncSession, plugin_id: uuid.UUID) -> AppiumPlugin | None:
    stmt = select(AppiumPlugin).where(AppiumPlugin.id == plugin_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def create_plugin(db: AsyncSession, data: PluginCreate) -> AppiumPlugin:
    plugin = AppiumPlugin(**data.model_dump())
    db.add(plugin)
    await db.commit()
    await db.refresh(plugin)
    return plugin


async def update_plugin(db: AsyncSession, plugin_id: uuid.UUID, data: PluginUpdate) -> AppiumPlugin | None:
    plugin = await get_plugin(db, plugin_id)
    if plugin is None:
        return None
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(plugin, field, value)
    await db.commit()
    await db.refresh(plugin)
    return plugin


async def delete_plugin(db: AsyncSession, plugin_id: uuid.UUID) -> bool:
    plugin = await get_plugin(db, plugin_id)
    if plugin is None:
        return False
    await db.delete(plugin)
    await db.commit()
    return True


async def fetch_host_plugins(host: Host) -> list[dict[str, str]]:
    return await list_agent_plugins(
        host.ip,
        host.agent_port,
        http_client_factory=httpx.AsyncClient,
    )


async def get_host_plugin_statuses(host: Host, plugins: list[AppiumPlugin]) -> list[dict[str, Any]]:
    plugins = _filter_enabled(plugins)
    installed = await fetch_host_plugins(host)
    installed_map = {plugin["name"]: plugin["version"] for plugin in installed}

    results: list[dict[str, Any]] = []
    for plugin in plugins:
        installed_ver = installed_map.get(plugin.name)
        if installed_ver is None:
            status = "missing"
        elif installed_ver == plugin.version:
            status = "ok"
        else:
            status = "mismatch"
        results.append(
            {
                "name": plugin.name,
                "required_version": plugin.version,
                "installed_version": installed_ver,
                "status": status,
                "enabled": plugin.enabled,
            }
        )
    return results


def _plugin_payload(plugin: AppiumPlugin) -> dict[str, Any]:
    return {
        "name": plugin.name,
        "version": plugin.version,
        "source": plugin.source,
        "package": plugin.package,
    }


async def sync_host_plugins(host: Host, plugins: list[AppiumPlugin]) -> dict[str, Any]:
    plugins = _filter_enabled(plugins)
    return await sync_agent_plugins(
        host.ip,
        host.agent_port,
        plugins=[_plugin_payload(plugin) for plugin in plugins],
        http_client_factory=httpx.AsyncClient,
    )


async def sync_all_host_plugins(db: AsyncSession) -> dict[str, Any]:
    stmt = select(Host).order_by(Host.hostname)
    result = await db.execute(stmt)
    hosts = list(result.scalars().all())
    plugins = await list_plugins(db)

    online_hosts: list[uuid.UUID] = []
    synced_hosts: list[uuid.UUID] = []
    failed_hosts: list[uuid.UUID] = []
    skipped_hosts: list[uuid.UUID] = []

    for host in hosts:
        if host.status != HostStatus.online:
            skipped_hosts.append(host.id)
            continue

        online_hosts.append(host.id)
        try:
            await sync_host_plugins(host, plugins)
        except (AgentCallError, httpx.HTTPError):
            failed_hosts.append(host.id)
        else:
            synced_hosts.append(host.id)

    return {
        "total_hosts": len(hosts),
        "online_hosts": online_hosts,
        "synced_hosts": synced_hosts,
        "failed_hosts": failed_hosts,
        "skipped_hosts": skipped_hosts,
    }


async def auto_sync_host_plugins(host: Host, plugins: list[AppiumPlugin]) -> None:
    if host.status.value != "online":
        return
    if not plugins:
        return
    try:
        await sync_host_plugins(host, plugins)
        logger.info("Auto-synced Appium plugins for host %s", host.hostname)
    except (AgentCallError, httpx.HTTPError) as exc:
        logger.warning("Automatic plugin sync failed for host %s: %s", host.hostname, exc)
