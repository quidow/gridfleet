from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.devices.models import DeviceGroup, DeviceGroupMemberOf, GroupType
from tests.concurrency.group_lock_helpers import capture_statements
from tests.helpers import create_device_record, create_host
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from uuid import UUID

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
        operational_state=str(payload.get("operational_state", "offline")),
        device_type=payload.get("device_type", "real_device"),
        connection_type=payload.get("connection_type"),
        ip_address=payload.get("ip_address"),
    )
    return {"id": str(device.id)}


async def _relation_targets(db_session: AsyncSession, dynamic_key: str) -> list[str]:
    """The static-group keys a dynamic group's ``device_group_member_of`` rows name.

    Reads the relation rather than the JSON column on purpose: from this phase on
    the two can disagree, and only the relation restricts membership.
    """
    source = aliased(DeviceGroup, name="source")
    stmt = (
        select(DeviceGroup.key)
        .join(DeviceGroupMemberOf, DeviceGroupMemberOf.static_group_id == DeviceGroup.id)
        .join(source, source.id == DeviceGroupMemberOf.dynamic_group_id)
        .where(source.key == dynamic_key)
    )
    return sorted((await db_session.execute(stmt)).scalars().all())


async def _stored_filters(db_session: AsyncSession, group_key: str) -> Any:  # noqa: ANN401 - raw JSONB value
    """The group's ``filters`` column exactly as stored, bypassing the identity map."""
    stmt = select(DeviceGroup.filters).where(DeviceGroup.key == group_key)
    return (await db_session.execute(stmt)).scalar_one()


async def _create_group(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    name = str(overrides.get("name", "Test Group"))
    payload: dict[str, Any] = {
        "key": name.lower().replace(" ", "-"),
        "name": name,
        "group_type": "static",
        **overrides,
    }
    resp = await client.post("/api/device-groups", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


async def test_create_static_group(client: AsyncClient) -> None:
    data = await _create_group(client)
    assert data["name"] == "Test Group"
    assert data["group_type"] == "static"
    assert data["key"] == "test-group"
    assert "id" not in data


async def test_group_key_is_public_immutable_identity(client: AsyncClient) -> None:
    created = await client.post(
        "/api/device-groups",
        json={"key": "east-lab", "name": "East lab", "group_type": "static"},
    )
    assert created.status_code == 201
    assert created.json()["key"] == "east-lab"
    assert "id" not in created.json()

    duplicate_name = await client.post(
        "/api/device-groups",
        json={"key": "west-lab", "name": "East lab", "group_type": "static"},
    )
    assert duplicate_name.status_code == 201
    assert (await client.get("/api/device-groups/east-lab")).status_code == 200
    assert (await client.patch("/api/device-groups/east-lab", json={"name": "East"})).status_code == 200
    assert (await client.patch("/api/device-groups/east-lab", json={"key": "renamed"})).status_code == 422


@pytest.mark.parametrize("key", ["East", "-east", "east-", "east_lab", "", "a" * 65])
async def test_create_group_rejects_malformed_key(client: AsyncClient, key: str) -> None:
    response = await client.post("/api/device-groups", json={"key": key, "name": "East lab", "group_type": "static"})
    assert response.status_code == 422


async def test_group_key_conflicts_and_unknown_keys_are_not_found(client: AsyncClient) -> None:
    await _create_group(client, key="east-lab", name="East lab")
    conflict = await client.post(
        "/api/device-groups", json={"key": "east-lab", "name": "Another east lab", "group_type": "static"}
    )
    assert conflict.status_code == 409
    assert (await client.get("/api/device-groups/unknown-lab")).status_code == 404


@pytest.mark.parametrize(
    ("method", "path", "json"),
    [
        ("GET", "/api/device-groups/East", None),
        ("PATCH", "/api/device-groups/East", {"name": "East"}),
        ("DELETE", "/api/device-groups/East", None),
        ("POST", "/api/device-groups/East/members", {"device_ids": []}),
        ("DELETE", "/api/device-groups/East/members", {"device_ids": []}),
        ("POST", "/api/device-groups/East/bulk/start-nodes", None),
        ("POST", "/api/device-groups/East/bulk/stop-nodes", None),
        ("POST", "/api/device-groups/East/bulk/restart-nodes", None),
        ("POST", "/api/device-groups/East/bulk/enter-maintenance", {"device_ids": []}),
        ("POST", "/api/device-groups/East/bulk/exit-maintenance", None),
        ("POST", "/api/device-groups/East/bulk/reconnect", None),
        ("POST", "/api/device-groups/East/bulk/delete", None),
    ],
)
async def test_group_routes_reject_malformed_keys(client: AsyncClient, method: str, path: str, json: object) -> None:
    assert (await client.request(method, path, json=json)).status_code == 422


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
    resp = await client.get(f"/api/device-groups/{group['key']}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Group"
    assert "devices" in data


async def test_get_group_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/device-groups/unknown-group")
    assert resp.status_code == 404


async def test_update_group(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.patch(
        f"/api/device-groups/{group['key']}",
        json={"name": "Updated Group", "description": "new desc"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Group"
    assert data["description"] == "new desc"


async def test_delete_group(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.delete(f"/api/device-groups/{group['key']}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/device-groups/{group['key']}")
    assert resp.status_code == 404


async def test_add_members(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    d1 = await _create_device(db_session, "grp-001", "D1", default_host_id)
    d2 = await _create_device(db_session, "grp-002", "D2", default_host_id)

    resp = await client.post(
        f"/api/device-groups/{group['key']}/members",
        json={"device_ids": [d1["id"], d2["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json()["added"] == 2

    # Verify members show in detail
    detail = await client.get(f"/api/device-groups/{group['key']}")
    assert detail.json()["device_count"] == 2


async def test_remove_members(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    d1 = await _create_device(db_session, "grp-003", "D3", default_host_id)

    await client.post(
        f"/api/device-groups/{group['key']}/members",
        json={"device_ids": [d1["id"]]},
    )
    resp = await client.request(
        "DELETE",
        f"/api/device-groups/{group['key']}/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 200
    assert resp.json()["removed"] == 1


async def test_add_members_to_dynamic_group_fails(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    group = await _create_group(client, name="Dynamic", group_type="dynamic", filters={"platform_id": "android_mobile"})
    d1 = await _create_device(db_session, "grp-dyn-001", "D-dyn", default_host_id)

    resp = await client.post(
        f"/api/device-groups/{group['key']}/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 400


async def test_remove_members_from_dynamic_group_fails(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    group = await _create_group(
        client, name="Dynamic Remove", group_type="dynamic", filters={"platform_id": "android_mobile"}
    )
    d1 = await _create_device(db_session, "grp-dyn-002", "D-dyn-rm", default_host_id)

    resp = await client.request(
        "DELETE",
        f"/api/device-groups/{group['key']}/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 400


async def test_add_members_to_unknown_group_is_404(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    d1 = await _create_device(db_session, "grp-missing-001", "D-missing", default_host_id)

    resp = await client.post(
        "/api/device-groups/no-such-group/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 404


async def test_remove_members_from_unknown_group_is_404(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    d1 = await _create_device(db_session, "grp-missing-002", "D-missing-rm", default_host_id)

    resp = await client.request(
        "DELETE",
        "/api/device-groups/no-such-group/members",
        json={"device_ids": [d1["id"]]},
    )
    assert resp.status_code == 404


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
        client, name="All Android", group_type="dynamic", filters={"platform_id": "android_mobile"}
    )

    detail = await client.get(f"/api/device-groups/{group['key']}")
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

    detail = await client.get(f"/api/device-groups/{group['key']}")
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


async def test_dynamic_group_resolves_identity_target_and_lifecycle(
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
        verified=False,
    )
    non_matching_lifecycle = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="dyn-shared-002",
        name="Lifecycle Miss",
        connection_target="10.10.0.2:5555",
        device_type="real_device",
        connection_type="network",
    )

    matching.lifecycle_policy_state = {
        "last_failure_reason": "ADB not responsive",
        "last_action": "auto_stop_deferred",
        "last_action_at": "2026-03-30T10:00:00+00:00",
        "deferred_stop": True,
        "deferred_stop_reason": "ADB not responsive",
        "deferred_stop_since": "2026-03-30T10:00:00+00:00",
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
            "status": "offline",
        },
    )

    detail = await client.get(f"/api/device-groups/{group['key']}")
    assert detail.status_code == 200
    data = detail.json()
    assert [device["id"] for device in data["devices"]] == [str(matching.id)]
    assert data["filters"]["identity_value"] == "dyn-shared-001"


async def test_group_bulk_restart_nodes(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    device = await _create_device(db_session, "grp-restart-001", "Restart Me", default_host_id)
    await client.post(f"/api/device-groups/{group['key']}/members", json={"device_ids": [device["id"]]})

    start_resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert start_resp.status_code == 200

    resp = await client.post(f"/api/device-groups/{group['key']}/bulk/restart-nodes")

    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 1


async def test_group_bulk_delete_devices(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    group = await _create_group(client)
    device = await _create_device(db_session, "grp-delete-001", "Delete Me", default_host_id)
    await client.post(f"/api/device-groups/{group['key']}/members", json={"device_ids": [device["id"]]})

    resp = await client.post(f"/api/device-groups/{group['key']}/bulk/delete")
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
    )
    await client.post(f"/api/device-groups/{group['key']}/members", json={"device_ids": [device["id"]]})

    with patch("app.devices.services.bulk.httpx.AsyncClient") as mock_client_cls:
        mock_client = mock_client_cls.return_value
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"success": True}
        mock_client.post = AsyncMock(return_value=mock_response)

        resp = await client.post(f"/api/device-groups/{group['key']}/bulk/reconnect")

    assert resp.status_code == 200
    assert resp.json()["succeeded"] == 1


async def test_group_bulk_set_status_route_removed(client: AsyncClient) -> None:
    group = await _create_group(client)
    resp = await client.post(
        f"/api/device-groups/{group['key']}/bulk/set-status",
        json={"device_ids": [], "status": "available"},
    )
    assert resp.status_code == 404


@pytest.mark.db
async def test_dynamic_group_member_of_anded_with_native_filters(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """member_of references static groups ANDed with native filters."""
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    await client.post("/api/device-groups", json={"key": "tv", "name": "TV", "group_type": "static"})

    east_tv = await _create_device(db_session, "mem-tv-1", "TV1", default_host_id, device_type="real_device")
    east_phone = await _create_device(db_session, "mem-phone-1", "Phone1", default_host_id, device_type="real_device")
    # Put devices in static groups via the members API.
    await client.post("/api/device-groups/east/members", json={"device_ids": [east_tv["id"], east_phone["id"]]})
    await client.post("/api/device-groups/tv/members", json={"device_ids": [east_tv["id"]]})

    resp = await client.post(
        "/api/device-groups",
        json={
            "key": "east-tvs",
            "name": "East TVs",
            "group_type": "dynamic",
            "filters": {"member_of": ["east", "tv"], "device_type": "real_device"},
        },
    )
    assert resp.status_code == 201

    detail = await client.get("/api/device-groups/east-tvs")
    assert detail.status_code == 200
    data = detail.json()
    assert [d["id"] for d in data["devices"]] == [east_tv["id"]]


@pytest.mark.db
async def test_dynamic_group_member_of_is_stored_as_relation_rows_not_json(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The wire shape is unchanged; the storage is not.

    ``filters.member_of`` still round-trips through create, read and list, but
    the reference lives in ``device_group_member_of`` and the JSON column keeps
    only the native axes.
    """
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    wire_filters = {"member_of": ["east"], "device_type": "real_device"}

    created = await client.post(
        "/api/device-groups",
        json={"key": "east-real", "name": "East real", "group_type": "dynamic", "filters": wire_filters},
    )
    assert created.status_code == 201, created.text
    assert created.json()["filters"] == wire_filters

    fetched = await client.get("/api/device-groups/east-real")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["filters"] == wire_filters

    listed = await client.get("/api/device-groups")
    assert {group["key"]: group.get("filters") for group in listed.json()}["east-real"] == wire_filters

    assert await _stored_filters(db_session, "east-real") == {"device_type": "real_device"}
    assert await _relation_targets(db_session, "east-real") == ["east"]


@pytest.mark.db
async def test_update_group_replaces_then_clears_member_of_relations(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    await client.post("/api/device-groups", json={"key": "west", "name": "West", "group_type": "static"})
    await client.post(
        "/api/device-groups",
        json={
            "key": "east-real",
            "name": "East real",
            "group_type": "dynamic",
            "filters": {"member_of": ["east"], "device_type": "real_device"},
        },
    )
    assert await _relation_targets(db_session, "east-real") == ["east"]

    swapped = await client.patch(
        "/api/device-groups/east-real",
        json={"filters": {"member_of": ["west"], "device_type": "real_device"}},
    )
    assert swapped.status_code == 200, swapped.text
    assert swapped.json()["filters"] == {"member_of": ["west"], "device_type": "real_device"}
    assert await _relation_targets(db_session, "east-real") == ["west"]

    cleared = await client.patch(
        "/api/device-groups/east-real",
        json={"filters": {"member_of": [], "device_type": "real_device"}},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["filters"] == {"device_type": "real_device"}
    assert await _relation_targets(db_session, "east-real") == []
    assert await _stored_filters(db_session, "east-real") == {"device_type": "real_device"}

    dropped = await client.patch("/api/device-groups/east-real", json={"filters": None})
    assert dropped.status_code == 200, dropped.text
    assert "filters" not in dropped.json()
    assert await _stored_filters(db_session, "east-real") is None
    assert await _relation_targets(db_session, "east-real") == []


@pytest.mark.db
async def test_update_group_without_a_filters_key_keeps_its_member_of_relations(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A rename must not silently drop the group's references."""
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    await client.post(
        "/api/device-groups",
        json={"key": "east-real", "name": "East real", "group_type": "dynamic", "filters": {"member_of": ["east"]}},
    )

    renamed = await client.patch("/api/device-groups/east-real", json={"name": "Renamed"})

    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["filters"] == {"member_of": ["east"]}
    assert await _relation_targets(db_session, "east-real") == ["east"]


@pytest.mark.db
async def test_create_dynamic_group_normalizes_duplicate_member_of_keys(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Duplicates collapse to a sorted set — the one intended wire change.

    The relation's composite primary key makes a repeated reference
    unrepresentable, and the evaluator has always read ``member_of`` as a set, so
    normalising on write loses nothing a caller could have depended on.
    """
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    await client.post("/api/device-groups", json={"key": "west", "name": "West", "group_type": "static"})

    created = await client.post(
        "/api/device-groups",
        json={
            "key": "dupes",
            "name": "Dupes",
            "group_type": "dynamic",
            "filters": {"member_of": ["west", "east", "east"]},
        },
    )

    assert created.status_code == 201, created.text
    assert created.json()["filters"] == {"member_of": ["east", "west"]}
    assert await _relation_targets(db_session, "dupes") == ["east", "west"]


@pytest.mark.db
@pytest.mark.parametrize("sent", [{}, {"member_of": []}])
async def test_empty_filters_normalize_to_an_absent_filters_key(
    client: AsyncClient,
    db_session: AsyncSession,
    sent: dict[str, Any],
) -> None:
    """The second wire change this phase makes, pinned rather than incidental.

    A dynamic group whose filters pin nothing used to store ``{}`` and answer
    ``"filters": {}``; it now stores SQL ``NULL`` and omits the key. ``{}`` and
    absent both mean "pins nothing", so collapsing them keeps one shape for one
    fact — which matters here because ``member_of`` is no longer part of that
    JSON, so ``{"member_of": [...]}`` reduces to the same empty payload.

    Migrated rows land on the same shape: revision ``6d8c3b5042b5`` leaves
    ``filters = {}`` on any dynamic group whose only axis was ``member_of``.
    """
    created = await client.post(
        "/api/device-groups",
        json={"key": "pins-nothing", "name": "Pins nothing", "group_type": "dynamic", "filters": sent},
    )
    assert created.status_code == 201, created.text
    assert "filters" not in created.json()
    assert await _stored_filters(db_session, "pins-nothing") is None

    fetched = await client.get("/api/device-groups/pins-nothing")
    assert fetched.status_code == 200, fetched.text
    assert "filters" not in fetched.json()

    patched = await client.patch("/api/device-groups/pins-nothing", json={"filters": sent})
    assert patched.status_code == 200, patched.text
    assert "filters" not in patched.json()
    assert await _stored_filters(db_session, "pins-nothing") is None

    # A row the migration left holding a literal ``{}`` reads the same way.
    db_session.add(DeviceGroup(key="migrated", name="Migrated", group_type=GroupType.dynamic, filters={}))
    await db_session.commit()
    migrated = await client.get("/api/device-groups/migrated")
    assert migrated.status_code == 200, migrated.text
    assert "filters" not in migrated.json()


@pytest.mark.db
async def test_legacy_json_member_of_is_never_echoed_back(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The migration deliberately leaves a static group's stored ``member_of`` alone.

    That JSON restricts nothing, so the serializer must drop it rather than
    advertise a reference the relation table does not carry.
    """
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    db_session.add(
        DeviceGroup(
            key="legacy-static",
            name="Legacy",
            group_type=GroupType.static,
            filters={"member_of": ["east"], "device_type": "real_device"},
        )
    )
    await db_session.commit()

    fetched = await client.get("/api/device-groups/legacy-static")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["filters"] == {"device_type": "real_device"}

    listed = await client.get("/api/device-groups")
    assert {group["key"]: group.get("filters") for group in listed.json()}["legacy-static"] == {
        "device_type": "real_device"
    }
    assert await _relation_targets(db_session, "legacy-static") == []


@pytest.mark.db
async def test_dynamic_group_member_of_unknown_key_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    resp = await client.post(
        "/api/device-groups",
        json={
            "key": "bad",
            "name": "Bad",
            "group_type": "dynamic",
            "filters": {"member_of": ["missing"]},
        },
    )
    assert resp.status_code == 422
    assert "missing" in resp.json()["error"]["message"]
    # Neither half of the write may survive a rejected reference.
    assert (await client.get("/api/device-groups/bad")).status_code == 404
    assert await _relation_targets(db_session, "bad") == []


@pytest.mark.db
async def test_dynamic_group_member_of_dynamic_key_rejected(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await client.post(
        "/api/device-groups",
        json={
            "key": "dyn-a",
            "name": "Dyn A",
            "group_type": "dynamic",
            "filters": {"device_type": "real_device"},
        },
    )
    resp = await client.post(
        "/api/device-groups",
        json={
            "key": "dyn-b",
            "name": "Dyn B",
            "group_type": "dynamic",
            "filters": {"member_of": ["dyn-a"]},
        },
    )
    assert resp.status_code == 422
    assert "dyn-a" in resp.json()["error"]["message"]
    assert (await client.get("/api/device-groups/dyn-b")).status_code == 404
    assert await _relation_targets(db_session, "dyn-b") == []


@pytest.mark.db
async def test_create_static_group_with_filters_rejected_as_422(client: AsyncClient) -> None:
    """A static group carrying filters is a domain validation failure, not a 500.

    Same class as ``member_of`` naming an unknown key: the body is schema-valid
    but the payload is invalid, so it must surface as 422 naming the problem.
    """
    resp = await client.post(
        "/api/device-groups",
        json={"key": "lab", "name": "Lab", "group_type": "static", "filters": {"pack_id": "appium-uiautomator2"}},
    )
    assert resp.status_code == 422, resp.text
    assert "filters" in resp.text.lower()
    assert "static" in resp.text.lower()


@pytest.mark.db
async def test_update_static_group_with_filters_rejected_as_422(client: AsyncClient) -> None:
    create = await client.post(
        "/api/device-groups",
        json={"key": "lab-patch", "name": "Lab", "group_type": "static"},
    )
    assert create.status_code == 201
    resp = await client.patch(
        "/api/device-groups/lab-patch",
        json={"filters": {"pack_id": "appium-uiautomator2"}},
    )
    assert resp.status_code == 422, resp.text
    assert "filters" in resp.text.lower()
    assert "static" in resp.text.lower()


@pytest.mark.db
async def test_delete_static_group_referenced_by_dynamic_returns_409(
    client: AsyncClient,
) -> None:
    await client.post("/api/device-groups", json={"key": "ref-static", "name": "Ref", "group_type": "static"})
    create = await client.post(
        "/api/device-groups",
        json={
            "key": "ref-dyn",
            "name": "Ref Dyn",
            "group_type": "dynamic",
            "filters": {"member_of": ["ref-static"]},
        },
    )
    assert create.status_code == 201
    resp = await client.delete("/api/device-groups/ref-static")
    assert resp.status_code == 409


@pytest.mark.db
async def test_delete_static_group_referenced_only_by_relation_rows_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The migrated shape: dependents exist as relation rows and nothing else.

    Revision ``6d8c3b5042b5`` emptied every dynamic group's ``filters.member_of``,
    so a dependent scan over the JSON column sees no referrer and lets the
    ``DELETE`` reach the RESTRICT foreign key as an untranslated
    ``IntegrityError`` — a 500 where the contract promises 409. Fixtures that
    build their groups through the API do not reproduce this; only rows shaped
    the way the migration leaves them do.
    """
    east = DeviceGroup(key="east", name="East", group_type=GroupType.static)
    zulu = DeviceGroup(
        key="zulu-dyn", name="Zulu", group_type=GroupType.dynamic, filters={"device_type": "real_device"}
    )
    alpha = DeviceGroup(key="alpha-dyn", name="Alpha", group_type=GroupType.dynamic, filters=None)
    db_session.add_all([east, zulu, alpha])
    await db_session.flush()
    db_session.add_all(
        [
            DeviceGroupMemberOf(dynamic_group_id=zulu.id, static_group_id=east.id),
            DeviceGroupMemberOf(dynamic_group_id=alpha.id, static_group_id=east.id),
        ]
    )
    await db_session.commit()

    resp = await client.delete("/api/device-groups/east")

    assert resp.status_code == 409, resp.text
    # Dependents stay ordered, so the operator-facing message is deterministic.
    assert resp.json()["error"]["message"].endswith("alpha-dyn, zulu-dyn")
    assert (await client.get("/api/device-groups/east")).status_code == 200


@pytest.mark.db
async def test_delete_dynamic_group_cascades_its_relation_rows_in_postgres(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The source side is ``ON DELETE CASCADE``; the service issues no cleanup."""
    await client.post("/api/device-groups", json={"key": "east", "name": "East", "group_type": "static"})
    await client.post(
        "/api/device-groups",
        json={"key": "east-real", "name": "East real", "group_type": "dynamic", "filters": {"member_of": ["east"]}},
    )
    assert await _relation_targets(db_session, "east-real") == ["east"]
    await db_session.rollback()

    async with capture_statements(db_session) as statements:
        resp = await client.delete("/api/device-groups/east-real")

    assert resp.status_code == 204, resp.text
    assert await _relation_targets(db_session, "east-real") == []
    normalized = [" ".join(statement.lower().split()) for statement in statements]
    assert not [s for s in normalized if "delete from device_group_member_of" in s], statements
    # Only the source's outgoing edges went away: the target is now deletable.
    assert (await client.delete("/api/device-groups/east")).status_code == 204


@pytest.mark.db
@pytest.mark.parametrize("stored", [{"member_of": ["target"]}, {"member_of": "target"}])
async def test_delete_is_not_blocked_by_inert_json_member_of(
    client: AsyncClient,
    db_session: AsyncSession,
    stored: dict[str, Any],
) -> None:
    """Stored JSON is not a reference from this phase on.

    Membership reads ``device_group_member_of``, so a dynamic row whose
    ``filters`` still names the target restricts nothing and must not block the
    target's deletion. Both shapes the old raw-dict scan matched — the list form
    and the legacy bare string — are equally inert now.
    """
    await client.post("/api/device-groups", json={"key": "target", "name": "Target", "group_type": "static"})
    db_session.add(DeviceGroup(key="legacy-dyn", name="Legacy", group_type=GroupType.dynamic, filters=stored))
    await db_session.commit()

    resp = await client.delete("/api/device-groups/target")

    assert resp.status_code == 204, resp.text


@pytest.mark.db
async def test_static_membership_mutation_preserves_device_state(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Group membership mutation is routing metadata only: no readiness/verified_at/node changes."""
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices.services import readiness as device_readiness

    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="preserve-1",
        connection_target="preserve-1",
        name="Preserve",
        verified=True,
        operational_state="available",
    )
    # Give it a node so we can assert desired_state/restart watermark are untouched.
    node = AppiumNode(
        device_id=device.id,
        port=4730,
        pid=9999,
        active_connection_target=device.connection_target,
        desired_state=AppiumDesiredState.running,
        desired_port=4730,
    )
    db_session.add(node)
    await db_session.commit()
    await db_session.refresh(device)
    await db_session.refresh(node)

    verified_before = device.verified_at
    readiness_before = await device_readiness.assess_device_async(db_session, device)
    desired_state_before = node.desired_state
    restart_watermark_before = node.restart_requested_at

    await client.post("/api/device-groups", json={"key": "preserve", "name": "Preserve", "group_type": "static"})
    add = await client.post("/api/device-groups/preserve/members", json={"device_ids": [str(device.id)]})
    assert add.status_code == 200
    remove = await client.request(
        "DELETE",
        "/api/device-groups/preserve/members",
        json={"device_ids": [str(device.id)]},
    )
    assert remove.status_code == 200

    await db_session.refresh(device)
    await db_session.refresh(node)
    assert device.verified_at == verified_before
    assert (await device_readiness.assess_device_async(db_session, device)) == readiness_before
    assert node.desired_state == desired_state_before
    assert node.restart_requested_at == restart_watermark_before


async def test_create_group_survives_a_peer_delete_landing_after_the_commit(client: AsyncClient) -> None:
    """A create that committed must report 201, not 404, if the row is deleted immediately after.

    The route used to re-read the row after the service committed and released
    the group lock, so a peer ``DELETE`` in that gap turned a create that had
    already succeeded — and already published ``device_group.updated`` — into a
    404. A client retrying that 404 either recreates a group the operator
    deliberately deleted or gets a 409 for a create it believes never happened.

    Stubbing ``get_group`` to ``None`` is that peer delete: the row is gone by
    the time anything could re-read it. The route must still describe what its
    own request did.
    """
    with patch(
        "app.devices.services.groups.DeviceGroupsService.get_group",
        AsyncMock(return_value=None),
    ):
        resp = await client.post(
            "/api/device-groups",
            json={"key": "vanishes", "name": "Vanishes", "group_type": "static"},
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["key"] == "vanishes"
    assert body["device_count"] == 0
    # Populated inside the service transaction; reading them here proves the
    # response needs no post-commit fetch.
    assert body["created_at"] and body["updated_at"]


async def test_update_group_survives_a_peer_delete_landing_after_the_commit(
    client: AsyncClient,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.devices.services.groups import DeviceGroupsService

    created = await _create_group(client, key="updated-then-deleted", group_type="dynamic")
    original_count = DeviceGroupsService.dynamic_device_count

    async def count_then_delete(
        self: DeviceGroupsService,
        db: AsyncSession,
        *,
        group_id: UUID,
        group_key: str,
    ) -> int | None:
        async with db_session_maker.begin() as peer:
            assert await self.delete_group(peer, group_key) is True
        return await original_count(self, db, group_id=group_id, group_key=group_key)

    monkeypatch.setattr(DeviceGroupsService, "dynamic_device_count", count_then_delete)

    response = await client.patch(f"/api/device-groups/{created['key']}", json={"name": "Updated"})

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Updated"
    assert (await client.get(f"/api/device-groups/{created['key']}")).status_code == 404


async def test_create_dynamic_group_reports_the_same_device_count_as_a_read(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    """The create response's device_count must agree with an immediate GET.

    A dynamic group's membership is derived from its filters over devices that
    already exist, so unlike a static group it is not empty at creation. The
    create path cannot assume 0 the way it can for statics, where membership
    rows reference an id nobody has seen yet.
    """
    await _create_device(db_session, "DYN-1", "dyn-device", default_host_id)

    created = await client.post(
        "/api/device-groups",
        json={
            "key": "dc-dyn",
            "name": "DC dyn",
            "group_type": "dynamic",
            "filters": {"platform_id": "android_mobile"},
        },
    )
    assert created.status_code == 201, created.text

    fetched = await client.get("/api/device-groups/dc-dyn")
    assert fetched.status_code == 200, fetched.text

    assert created.json()["device_count"] == fetched.json()["device_count"], (
        f"create said {created.json()['device_count']}, read says {fetched.json()['device_count']}"
    )
    assert fetched.json()["device_count"] == 1
