from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from tests.helpers import create_device_record, create_host
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

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
async def test_dynamic_group_member_of_unknown_key_rejected(client: AsyncClient) -> None:
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


@pytest.mark.db
async def test_dynamic_group_member_of_dynamic_key_rejected(client: AsyncClient) -> None:
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
async def test_delete_unrelated_group_not_blocked_by_malformed_member_of_row(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A malformed stored ``member_of`` that does not name the target must not
    block an unrelated delete.

    The old scan validated every candidate through ``DeviceGroupFilters``
    (``extra="forbid"``), so one bare-string ``member_of`` row anywhere in the
    fleet 422'd every unrelated delete. The raw-dict check skips rows whose
    ``member_of`` cannot reference the target; the malformed row still surfaces
    later when ``get_group``/``list_groups`` serialize it.
    """
    from app.devices.models.group import DeviceGroup, GroupType

    await client.post("/api/device-groups", json={"key": "unrelated", "name": "Unrelated", "group_type": "static"})
    db_session.add(
        DeviceGroup(
            key="malformed-dyn",
            name="Malformed",
            group_type=GroupType.dynamic,
            filters={"member_of": "other-group"},  # bare string, does not name "unrelated"
        )
    )
    await db_session.commit()
    resp = await client.delete("/api/device-groups/unrelated")
    assert resp.status_code == 204, resp.text


@pytest.mark.db
async def test_delete_group_referenced_by_bare_string_member_of_returns_409(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """A legacy bare-string ``member_of`` naming the target still blocks the delete.

    The raw-dict check matches both the list form and the bare-string form, so a
    malformed referencer cannot silently leave a dangling ``member_of`` when its
    target is deleted.
    """
    from app.devices.models.group import DeviceGroup, GroupType

    await client.post("/api/device-groups", json={"key": "target", "name": "Target", "group_type": "static"})
    db_session.add(
        DeviceGroup(
            key="bare-dyn",
            name="Bare",
            group_type=GroupType.dynamic,
            filters={"member_of": "target"},  # bare string naming the target
        )
    )
    await db_session.commit()
    resp = await client.delete("/api/device-groups/target")
    assert resp.status_code == 409, resp.text


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
