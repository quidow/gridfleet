from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_device_record
from tests.pack.factories import seed_test_packs

DEVICE_PAYLOAD = {
    "identity_value": "emulator-5554",
    "name": "Pixel 7 Emulator",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
}

MOCK_GRID_STATUS = {
    "value": {
        "ready": True,
        "message": "Selenium Grid ready.",
        "nodes": [],
    }
}


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    """Seed driver packs so the assert_runnable gate passes in all tests."""
    await seed_test_packs(db_session)
    await db_session.commit()


def _mock_agent_response(json_data: dict[str, Any], status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _mock_agent_client(*, post_responses: list[MagicMock], get_responses: list[MagicMock] | None = None) -> MagicMock:
    mock_client = MagicMock()
    mock_client.post = AsyncMock(side_effect=post_responses)
    mock_client.get = AsyncMock(side_effect=get_responses or [])
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


async def test_grid_status(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["identity_value"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )

    with patch("app.routers.grid.grid_service.get_grid_status", return_value=MOCK_GRID_STATUS):
        resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["grid"] == MOCK_GRID_STATUS
    assert data["registry"]["device_count"] == 1
    assert data["registry"]["devices"][0]["identity_value"] == "emulator-5554"
    assert data["registry"]["devices"][0]["platform_id"] == "android_mobile"
    assert "platform" not in data["registry"]["devices"][0]
    assert data["registry"]["devices"][0]["node_state"] is None


async def test_grid_status_hub_unreachable(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["identity_value"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )

    with patch(
        "app.routers.grid.grid_service.get_grid_status", return_value={"ready": False, "error": "Connection refused"}
    ):
        resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    assert data["grid"]["ready"] is False
    assert data["registry"]["device_count"] == 1


async def test_grid_status_with_running_node(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """Grid status should reflect node state from the registry."""
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value=DEVICE_PAYLOAD["identity_value"],
        connection_target=DEVICE_PAYLOAD["identity_value"],
        name=DEVICE_PAYLOAD["name"],
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
        availability_status="available",
    )

    mock_client = _mock_agent_client(
        post_responses=[
            _mock_agent_response({"pid": 9999, "port": 4723, "connection_target": DEVICE_PAYLOAD["identity_value"]})
        ],
        get_responses=[_mock_agent_response({"running": True, "port": 4723})],
    )

    with (
        patch("app.services.node_service.httpx.AsyncClient", return_value=mock_client),
        patch("app.routers.grid.grid_service.get_grid_status", return_value=MOCK_GRID_STATUS),
    ):
        await client.post(f"/api/devices/{device.id}/node/start")
        resp = await client.get("/api/grid/status")

    assert resp.status_code == 200
    data = resp.json()
    dev_entry = data["registry"]["devices"][0]
    assert dev_entry["platform_id"] == "android_mobile"
    assert "platform" not in dev_entry
    assert dev_entry["node_state"] == "running"
    assert dev_entry["node_port"] == 4723
    assert dev_entry["availability_status"] == "available"
