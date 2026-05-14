from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import httpx

from app.errors import AgentCallError
from app.models.appium_plugin import AppiumPlugin
from app.models.host import Host, HostStatus, OSType
from app.schemas.plugin import PluginCreate, PluginUpdate
from app.services import plugin_service
from app.webhooks import service as webhook_service
from app.webhooks.schemas import WebhookCreate, WebhookUpdate

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def _host(db_session: AsyncSession, *, hostname: str, status: HostStatus) -> Host:
    host = Host(
        hostname=hostname,
        ip=f"10.30.0.{len(hostname)}",
        os_type=OSType.linux,
        agent_port=5100,
        status=status,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)
    return host


async def test_plugin_service_crud_and_missing_paths(db_session: AsyncSession) -> None:
    created = await plugin_service.create_plugin(
        db_session,
        PluginCreate(
            name="images",
            version="2.0.0",
            source="npm:@appium/images-plugin",
            package="@appium/images-plugin",
            enabled=True,
            notes="image matching",
        ),
    )

    assert (await plugin_service.get_plugin(db_session, created.id)).name == "images"
    assert [plugin.name for plugin in await plugin_service.list_plugins(db_session)] == ["images"]

    updated = await plugin_service.update_plugin(db_session, created.id, PluginUpdate(enabled=False, notes="paused"))
    assert updated is not None
    assert updated.enabled is False
    assert updated.notes == "paused"

    assert await plugin_service.update_plugin(db_session, uuid4_not_in_db(), PluginUpdate(version="2.1.0")) is None
    assert await plugin_service.delete_plugin(db_session, uuid4_not_in_db()) is False
    assert await plugin_service.delete_plugin(db_session, created.id) is True
    assert await plugin_service.list_plugins(db_session) == []


async def test_host_plugin_statuses_classify_installed_versions() -> None:
    host = Host(hostname="status-host", ip="10.30.0.20", os_type=OSType.linux, agent_port=5100)
    plugins = [
        AppiumPlugin(name="ok-plugin", version="1.0.0", source="npm:ok", enabled=True),
        AppiumPlugin(name="old-plugin", version="2.0.0", source="npm:old", enabled=True),
        AppiumPlugin(name="missing-plugin", version="3.0.0", source="npm:missing", enabled=True),
        AppiumPlugin(name="disabled-plugin", version="9.0.0", source="npm:disabled", enabled=False),
    ]

    with patch(
        "app.services.plugin_service.fetch_host_plugins",
        new=AsyncMock(
            return_value=[
                {"name": "ok-plugin", "version": "1.0.0"},
                {"name": "old-plugin", "version": "1.9.0"},
            ]
        ),
    ):
        statuses = await plugin_service.get_host_plugin_statuses(host, plugins)

    assert [status["name"] for status in statuses] == ["ok-plugin", "old-plugin", "missing-plugin"]
    assert [status["status"] for status in statuses] == ["ok", "mismatch", "missing"]
    assert statuses[1]["installed_version"] == "1.9.0"


async def test_sync_host_plugins_sends_enabled_payload_only() -> None:
    host = Host(hostname="sync-host", ip="10.30.0.21", os_type=OSType.linux, agent_port=5100)
    plugins = [
        AppiumPlugin(name="execute-driver", version="1.0.0", source="npm:execute", package="execute", enabled=True),
        AppiumPlugin(name="disabled", version="1.0.0", source="npm:disabled", package=None, enabled=False),
    ]

    with patch(
        "app.services.plugin_service.sync_agent_plugins",
        new=AsyncMock(return_value={"installed": ["execute-driver"], "updated": [], "removed": [], "errors": {}}),
    ) as sync_agent:
        result = await plugin_service.sync_host_plugins(host, plugins)

    assert result["installed"] == ["execute-driver"]
    sync_agent.assert_awaited_once()
    assert sync_agent.await_args.kwargs["plugins"] == [
        {"name": "execute-driver", "version": "1.0.0", "source": "npm:execute", "package": "execute"}
    ]


async def test_sync_all_host_plugins_reports_synced_failed_and_skipped_hosts(db_session: AsyncSession) -> None:
    online = await _host(db_session, hostname="online-plugin-host", status=HostStatus.online)
    failing = await _host(db_session, hostname="failing-plugin-host", status=HostStatus.online)
    offline = await _host(db_session, hostname="offline-plugin-host", status=HostStatus.offline)
    db_session.add(AppiumPlugin(name="images", version="2.0.0", source="npm:images", enabled=True))
    await db_session.commit()

    async def fake_sync(host: Host, plugins: list[AppiumPlugin]) -> dict[str, Any]:
        assert [plugin.name for plugin in plugins] == ["images"]
        if host.id == failing.id:
            raise AgentCallError(host.ip, "agent failed")
        return {"installed": [host.hostname]}

    with patch("app.services.plugin_service.sync_host_plugins", new=fake_sync):
        result = await plugin_service.sync_all_host_plugins(db_session)

    assert result == {
        "total_hosts": 3,
        "online_hosts": [failing.id, online.id],
        "synced_hosts": [online.id],
        "failed_hosts": [failing.id],
        "skipped_hosts": [offline.id],
    }


async def test_auto_sync_host_plugins_handles_non_actionable_and_agent_errors() -> None:
    online = Host(
        hostname="auto-online", ip="10.30.0.30", os_type=OSType.linux, agent_port=5100, status=HostStatus.online
    )
    offline = Host(
        hostname="auto-offline", ip="10.30.0.31", os_type=OSType.linux, agent_port=5100, status=HostStatus.offline
    )
    plugin = AppiumPlugin(name="images", version="2.0.0", source="npm:images", enabled=True)

    with patch("app.services.plugin_service.sync_host_plugins", new=AsyncMock()) as sync_host:
        await plugin_service.auto_sync_host_plugins(offline, [plugin])
        await plugin_service.auto_sync_host_plugins(online, [])
    sync_host.assert_not_awaited()

    with patch(
        "app.services.plugin_service.sync_host_plugins",
        new=AsyncMock(side_effect=httpx.ConnectError("offline")),
    ) as sync_host:
        await plugin_service.auto_sync_host_plugins(online, [plugin])
    sync_host.assert_awaited_once()


async def test_webhook_service_crud_filters_and_missing_paths(db_session: AsyncSession) -> None:
    enabled = await webhook_service.create_webhook(
        db_session,
        WebhookCreate(
            name="alerts",
            url="https://example.test/alerts",
            event_types=["webhook.test", "node.crash"],
            enabled=True,
        ),
    )
    disabled = await webhook_service.create_webhook(
        db_session,
        WebhookCreate(
            name="audit",
            url="https://example.test/audit",
            event_types=["settings.changed"],
            enabled=False,
        ),
    )

    assert [webhook.name for webhook in await webhook_service.list_webhooks(db_session)] == ["alerts", "audit"]
    assert [webhook.name for webhook in await webhook_service.list_webhooks(db_session, enabled=True)] == ["alerts"]
    assert [webhook.name for webhook in await webhook_service.list_webhooks(db_session, enabled=False)] == ["audit"]

    updated = await webhook_service.update_webhook(db_session, disabled.id, WebhookUpdate(enabled=True, name="audit-2"))
    assert updated is not None
    assert updated.enabled is True
    assert updated.name == "audit-2"

    assert await webhook_service.get_webhook(db_session, enabled.id) is not None
    assert await webhook_service.update_webhook(db_session, uuid4_not_in_db(), WebhookUpdate(enabled=False)) is None
    assert await webhook_service.delete_webhook(db_session, uuid4_not_in_db()) is False
    assert await webhook_service.delete_webhook(db_session, enabled.id) is True


def uuid4_not_in_db() -> uuid.UUID:
    return uuid.uuid4()
