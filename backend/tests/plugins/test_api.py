from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.hosts.models import Host, HostStatus, OSType
from app.plugins.models import AppiumPlugin


async def _create_host(
    db_session: AsyncSession,
    *,
    hostname: str,
    ip: str,
    status: HostStatus,
    os_type: OSType = OSType.linux,
) -> Host:
    host = Host(
        hostname=hostname,
        ip=ip,
        os_type=os_type,
        agent_port=5100,
        status=status,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)
    return host


async def test_plugin_crud(client: AsyncClient) -> None:
    create_resp = await client.post(
        "/api/plugins",
        json={
            "name": "execute-driver",
            "version": "1.0.0",
            "source": "npm:@appium/execute-driver-plugin",
            "package": None,
            "enabled": True,
            "notes": "",
        },
    )

    assert create_resp.status_code == 201
    plugin_id = create_resp.json()["id"]

    update_resp = await client.patch(f"/api/plugins/{plugin_id}", json={"version": "1.0.1"})
    assert update_resp.status_code == 200
    assert update_resp.json()["version"] == "1.0.1"

    list_resp = await client.get("/api/plugins")
    assert list_resp.status_code == 200
    assert [plugin["name"] for plugin in list_resp.json()] == ["execute-driver"]

    delete_resp = await client.delete(f"/api/plugins/{plugin_id}")
    assert delete_resp.status_code == 204


async def test_sync_host_plugins_dispatches_enabled_only(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host = await _create_host(db_session, hostname="plugin-sync-host", ip="10.0.1.11", status=HostStatus.online)
    db_session.add_all(
        [
            AppiumPlugin(
                name="execute-driver",
                version="1.0.0",
                source="npm:@appium/execute-driver-plugin",
                enabled=True,
            ),
            AppiumPlugin(
                name="disabled-plugin",
                version="1.0.0",
                source="npm:disabled-plugin",
                enabled=False,
            ),
        ]
    )
    await db_session.commit()

    with patch(
        "app.plugins.service.sync_agent_plugins",
        new=AsyncMock(return_value={"installed": ["execute-driver"], "updated": [], "removed": [], "errors": {}}),
    ) as sync_agent:
        resp = await client.post(f"/api/hosts/{host.id}/plugins/sync")

    assert resp.status_code == 200
    sync_agent.assert_awaited_once()
    assert [plugin["name"] for plugin in sync_agent.await_args.kwargs["plugins"]] == ["execute-driver"]
