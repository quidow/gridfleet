from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs

HOST_PAYLOAD = {
    "hostname": "group-host",
    "ip": "10.0.0.50",
    "os_type": "linux",
    "agent_port": 5100,
}


@pytest_asyncio.fixture(autouse=True)
async def seed_packs(db_session: AsyncSession) -> None:
    """Seed driver packs so the assert_runnable gate passes in all tests."""
    await seed_test_packs(db_session)
    await db_session.commit()


@pytest_asyncio.fixture
async def default_host_id(client: AsyncClient) -> str:
    host = await create_host(client, **HOST_PAYLOAD)
    return str(host["id"])


async def _create_device(
    db_session: AsyncSession,
    identity_value: str,
    name: str,
    host_id: str,
    **overrides: object,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "identity_value": identity_value,
        "connection_target": identity_value,
        "name": name,
        "pack_id": "appium-uiautomator2",
        "platform_id": "android_mobile",
        "identity_scheme": "android_serial",
        "identity_scope": "host",
        "os_version": "14",
        "host_id": host_id,
        **overrides,
    }
    allow_resolution = bool(payload.pop("allow_android_network_identity_resolution", False))
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=str(payload["identity_value"]),
        connection_target=payload.get("connection_target"),
        name=str(payload["name"]),
        pack_id=str(payload["pack_id"]),
        platform_id=str(payload["platform_id"]),
        identity_scheme=str(payload["identity_scheme"]),
        identity_scope=str(payload["identity_scope"]),
        os_version=str(payload["os_version"]),
        availability_status=payload.get("availability_status", "offline"),
        device_type=payload.get("device_type", "real_device"),
        connection_type=payload.get("connection_type"),
        ip_address=payload.get("ip_address"),
        allow_android_network_identity_resolution=allow_resolution,
    )
    return {"id": str(device.id)}


async def _create_group(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": "Test Group", "group_type": "static", **overrides}
    resp = await client.post("/api/device-groups", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


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


async def test_create_static_group(client: AsyncClient) -> None:
    data = await _create_group(client)
    assert data["name"] == "Test Group"
    assert data["group_type"] == "static"
    assert "id" in data


async def test_create_dynamic_group(client: AsyncClient) -> None:
    data = await _create_group(
        client,
        name="Android Devices",
        group_type="dynamic",
        filters={"platform_id": "android_mobile"},
    )
    assert data["name"] == "Android Devices"
    assert data["group_type"] == "dynamic"
    assert data["filters"] == {"platform_id": "android_mobile"}


async def test_list_groups(client: AsyncClient) -> None:
    await _create_group(client, name="group-a")
    await _create_group(client, name="group-b")

    resp = await client.get("/api/device-groups")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_get_group(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.get(f"/api/device-groups/{group['id']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Group"
    assert "devices" in data


async def test_get_group_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/device-groups/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_update_group(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.patch(
        f"/api/device-groups/{group['id']}",
        json={"name": "Updated Group", "description": "new desc"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Group"
    assert data["description"] == "new desc"


async def test_delete_group(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.delete(f"/api/device-groups/{group['id']}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/device-groups/{group['id']}")
    assert resp.status_code == 404


async def test_add_members(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    d1 = await _create_device(db_session, "grp-001", "D1", default_host_id)
    d2 = await _create_device(db_session, "grp-002", "D2", default_host_id)

    resp = await client.post(
        f"/api/device-groups/{group['id']}/members",
        json={"device_ids": [d1["id"], d2["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 2

    # Verify members show in detail
    detail = await client.get(f"/api/device-groups/{group['id']}")
    assert detail.json()["device_count"] == 2


async def test_remove_members(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    d1 = await _create_device(db_session, "grp-003", "D3", default_host_id)

    await client.post(
        f"/api/device-groups/{group['id']}/members",
        json={"device_ids": [d1["id"]]},
    )
    resp = await client.request(
        "DELETE",
        f"/api/device-groups/{group['id']}/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1


async def test_add_members_to_dynamic_group_fails(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    group = await _create_group(
        client,
        name="Dynamic",
        group_type="dynamic",
        filters={"platform_id": "android_mobile"},
    )
    d1 = await _create_device(db_session, "grp-dyn-001", "D-dyn", default_host_id)

    resp = await client.post(
        f"/api/device-groups/{group['id']}/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 400


async def test_dynamic_group_resolves_members(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(db_session, "dyn-001", "Android1", default_host_id)
    await _create_device(
        db_session,
        "dyn-002",
        "iOS1",
        default_host_id,
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
    )

    group = await _create_group(
        client,
        name="All Android",
        group_type="dynamic",
        filters={"platform_id": "android_mobile"},
    )

    detail = await client.get(f"/api/device-groups/{group['id']}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["device_count"] == 1
    assert data["devices"][0]["platform_id"] == "android_mobile"
    assert data["filters"] == {"platform_id": "android_mobile"}


async def test_dynamic_group_filters_by_pack_id(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(db_session, "dyn-pack-android", "Android Pack", default_host_id)
    await _create_device(
        db_session,
        "dyn-pack-ios",
        "iOS Pack",
        default_host_id,
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
    )

    group = await _create_group(
        client,
        name="Android Pack Devices",
        group_type="dynamic",
        filters={"pack_id": "appium-uiautomator2"},
    )

    detail = await client.get(f"/api/device-groups/{group['id']}")
    assert detail.status_code == 200
    data = detail.json()
    assert data["device_count"] == 1
    assert {item["pack_id"] for item in data["devices"]} == {"appium-uiautomator2"}
    assert data["filters"] == {"pack_id": "appium-uiautomator2"}


async def test_create_group_rejects_legacy_filter_rules_field(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/device-groups",
        json={"name": "Legacy Dynamic", "group_type": "dynamic", "filter_rules": {"platform": "android_mobile"}},
    )

    assert resp.status_code == 422


async def test_dynamic_group_resolves_identity_target_lifecycle_and_tags(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    matching = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="dyn-shared-001",
        name="Shared Match",
        connection_target="10.10.0.1:5555",
        device_type="real_device",
        connection_type="network",
        tags={"team": "qa", "lane": "smoke"},
    )
    non_matching_lifecycle = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="dyn-shared-002",
        name="Lifecycle Miss",
        connection_target="10.10.0.2:5555",
        device_type="real_device",
        connection_type="network",
        tags={"team": "qa", "lane": "smoke"},
    )

    matching.lifecycle_policy_state = {
        "last_failure_reason": "ADB not responsive",
        "last_action": "auto_stop_deferred",
        "last_action_at": "2026-03-30T10:00:00+00:00",
        "stop_pending": True,
        "stop_pending_reason": "ADB not responsive",
        "stop_pending_since": "2026-03-30T10:00:00+00:00",
        "recovery_suppressed_reason": None,
        "backoff_until": None,
        "recovery_backoff_attempts": 0,
    }
    non_matching_lifecycle.lifecycle_policy_state = {}
    await db_session.commit()

    group = await _create_group(
        client,
        name="Shared Filters",
        group_type="dynamic",
        filters={
            "host_id": default_host_id,
            "identity_value": "dyn-shared-001",
            "connection_target": "10.10.0.1:5555",
            "device_type": "real_device",
            "connection_type": "network",
            "availability_status": "offline",
            "tags": {"team": "qa", "lane": "smoke"},
        },
    )

    detail = await client.get(f"/api/device-groups/{group['id']}")
    assert detail.status_code == 200
    data = detail.json()
    assert [device["id"] for device in data["devices"]] == [str(matching.id)]
    assert data["filters"]["tags"] == {"team": "qa", "lane": "smoke"}


async def test_group_bulk_restart_nodes(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    device = await _create_device(db_session, "grp-restart-001", "Restart Me", default_host_id)
    await client.post(f"/api/device-groups/{group['id']}/members", json={"device_ids": [device["id"]]})

    mock_client = _mock_agent_client(
        post_responses=[
            _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "grp-restart-001"}),
            _mock_agent_response({"stopped": True, "port": 4723}),
            _mock_agent_response({"pid": 12346, "port": 4723, "connection_target": "grp-restart-001"}),
        ],
        get_responses=[
            _mock_agent_response({"running": True, "port": 4723}),
            _mock_agent_response({"running": True, "port": 4723}),
        ],
    )

    with patch("app.services.node_manager.httpx.AsyncClient", return_value=mock_client):
        start_resp = await client.post(f"/api/devices/{device['id']}/node/start")
        assert start_resp.status_code == 200

        resp = await client.post(f"/api/device-groups/{group['id']}/bulk/restart-nodes")

    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 1


async def test_group_bulk_update_tags(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    device = await _create_device(db_session, "grp-tags-001", "Tags Me", default_host_id)
    await client.post(f"/api/device-groups/{group['id']}/members", json={"device_ids": [device["id"]]})

    resp = await client.post(
        f"/api/device-groups/{group['id']}/bulk/update-tags",
        json={"device_ids": [], "tags": {"team": "qa"}, "merge": True},
    )
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 1

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.json()["tags"]["team"] == "qa"


async def test_group_bulk_delete_devices(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    device = await _create_device(db_session, "grp-delete-001", "Delete Me", default_host_id)
    await client.post(f"/api/device-groups/{group['id']}/members", json={"device_ids": [device["id"]]})

    resp = await client.post(f"/api/device-groups/{group['id']}/bulk/delete")
    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 1

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 404


async def test_group_bulk_reconnect(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await create_host(client, hostname="agent-1", ip="10.0.0.5", os_type="linux")
    group = await _create_group(client)
    device = await _create_device(
        db_session,
        "192.168.1.20:5555",
        "Reconnect Me",
        host["id"],
        device_type="real_device",
        connection_type="network",
        ip_address="192.168.1.20",
        allow_android_network_identity_resolution=True,
    )
    await client.post(f"/api/device-groups/{group['id']}/members", json={"device_ids": [device["id"]]})

    with patch("app.services.bulk_service.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"success": True}
        mock_client.post = AsyncMock(return_value=mock_response)

        resp = await client.post(f"/api/device-groups/{group['id']}/bulk/reconnect")

    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 1


async def test_group_bulk_set_status_route_removed(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.post(
        f"/api/device-groups/{group['id']}/bulk/set-status",
        json={"device_ids": [], "status": "available"},
    )
    assert resp.status_code == 404
