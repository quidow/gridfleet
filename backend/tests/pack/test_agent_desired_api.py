import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.host import Host, HostStatus, OSType
from tests.pack.factories import seed_test_packs


@pytest.mark.asyncio
async def test_desired_returns_enabled_pack(client: AsyncClient, db_session: AsyncSession) -> None:
    await seed_test_packs(db_session)
    host = Host(
        hostname="h1.local",
        ip="10.0.0.1",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()

    resp = await client.get("/agent/driver-packs/desired", params={"host_id": str(host.id)})
    assert resp.status_code == 200
    body = resp.json()
    pack = next(p for p in body["packs"] if p["id"] == "appium-uiautomator2")
    assert pack["release"] == "2026.04.0"
    assert pack["appium_server"]["package"] == "appium"
    assert pack["appium_driver"]["package"] == "appium-uiautomator2-driver"
