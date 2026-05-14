from typing import Any
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AgentUnreachableError
from tests.helpers import create_device_record

HOST_PAYLOAD = {
    "hostname": "mac-lab-01",
    "ip": "192.168.1.200",
    "os_type": "macos",
    "agent_port": 5100,
}

PACK_DEVICES_RESPONSE: dict[str, Any] = {
    "candidates": [
        {
            "pack_id": "appium-xcuitest",
            "platform_id": "ios",
            "identity_scheme": "apple_udid",
            "identity_scope": "global",
            "identity_value": "ABC-123",
            "suggested_name": "iPhone 15",
            "detected_properties": {
                "connection_target": "ABC-123",
                "os_version": "17.4",
            },
            "runnable": True,
        },
        {
            "pack_id": "appium-xcuitest",
            "platform_id": "tvos",
            "identity_scheme": "apple_udid",
            "identity_scope": "global",
            "identity_value": "DEF-456",
            "suggested_name": "Apple TV",
            "detected_properties": {
                "connection_target": "DEF-456",
                "os_version": "17.2",
            },
            "runnable": True,
        },
    ],
}

ANDROID_AVD_PACK_RESPONSE: dict[str, Any] = {
    "candidates": [
        {
            "pack_id": "appium-uiautomator2",
            "platform_id": "android_mobile",
            "identity_scheme": "android_serial",
            "identity_scope": "host",
            "identity_value": "avd:Pixel_8_API_35",
            "suggested_name": "Pixel 8 API 35",
            "detected_properties": {
                "connection_target": "Pixel_8_API_35",
                "os_version": "15",
                "device_type": "emulator",
                "connection_type": "virtual",
            },
            "runnable": True,
        }
    ],
}


def _patch_pack_devices(response: dict[str, Any]) -> object:
    return patch("app.hosts.router.get_pack_devices", new=AsyncMock(return_value=response))


async def test_discover_devices(client: AsyncClient) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    with _patch_pack_devices(PACK_DEVICES_RESPONSE):
        resp = await client.post(f"/api/hosts/{host['id']}/discover")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["new_devices"]) == 2
    assert data["new_devices"][0]["identity_value"] == "ABC-123"
    assert len(data["removed_identity_values"]) == 0


async def test_intake_candidates_marks_registered_devices(client: AsyncClient, db_session: AsyncSession) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()
    existing = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="ABC-123",
        connection_target="ABC-123",
        name="Existing iPhone 15",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.4",
    )

    with _patch_pack_devices(PACK_DEVICES_RESPONSE):
        resp = await client.get(f"/api/hosts/{host['id']}/intake-candidates")

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]["already_registered"] is True
    assert data[0]["registered_device_id"] == str(existing.id)
    assert data[1]["already_registered"] is False


async def test_intake_candidates_keep_android_avd_matching_host_scoped(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host_one = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()
    host_two = (
        await client.post(
            "/api/hosts",
            json={**HOST_PAYLOAD, "hostname": "mac-lab-02", "ip": "192.168.1.201"},
        )
    ).json()
    existing = await create_device_record(
        db_session,
        host_id=host_one["id"],
        identity_value="avd:Pixel_8_API_35",
        connection_target="Pixel_8_API_35",
        name="Existing Pixel 8 API 35",
        platform_id="android_mobile",
        os_version="15",
        device_type="emulator",
    )

    with _patch_pack_devices(ANDROID_AVD_PACK_RESPONSE):
        host_one_resp = await client.get(f"/api/hosts/{host_one['id']}/intake-candidates")
        host_two_resp = await client.get(f"/api/hosts/{host_two['id']}/intake-candidates")

    assert host_one_resp.status_code == 200
    assert host_two_resp.status_code == 200

    host_one_data = host_one_resp.json()
    host_two_data = host_two_resp.json()
    assert host_one_data[0]["connection_type"] == "virtual"
    assert host_one_data[0]["already_registered"] is True
    assert host_one_data[0]["registered_device_id"] == str(existing.id)
    assert host_two_data[0]["connection_type"] == "virtual"
    assert host_two_data[0]["already_registered"] is False
    assert host_two_data[0]["registered_device_id"] is None


async def test_discover_detects_removed(client: AsyncClient, db_session: AsyncSession) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="OLD-DEVICE",
        connection_target="OLD-DEVICE",
        name="Old Device",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="16.0",
    )

    empty_response: dict[str, Any] = {"candidates": []}
    with _patch_pack_devices(empty_response):
        resp = await client.post(f"/api/hosts/{host['id']}/discover")

    assert resp.status_code == 200
    data = resp.json()
    assert "OLD-DEVICE" in data["removed_identity_values"]


async def test_discover_treats_same_value_different_scheme_as_new(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="SHARED-IDENTITY",
        connection_target="SHARED-IDENTITY",
        name="Existing custom device",
        pack_id="custom-pack-a",
        platform_id="custom-platform-a",
        identity_scheme="custom_scheme_a",
        identity_scope="host",
        os_version="1.0",
    )

    same_value_different_scheme_response: dict[str, Any] = {
        "candidates": [
            {
                "pack_id": "custom-pack-b",
                "platform_id": "custom-platform-b",
                "identity_scheme": "custom_scheme_b",
                "identity_scope": "host",
                "identity_value": "SHARED-IDENTITY",
                "suggested_name": "New custom device",
                "detected_properties": {
                    "connection_target": "SHARED-IDENTITY",
                    "os_version": "2.0",
                },
                "runnable": True,
            }
        ],
    }

    with _patch_pack_devices(same_value_different_scheme_response):
        resp = await client.post(f"/api/hosts/{host['id']}/discover")

    assert resp.status_code == 200
    data = resp.json()
    assert [d["identity_scheme"] for d in data["new_devices"]] == ["custom_scheme_b"]
    assert data["updated_devices"] == []


async def test_confirm_discovery_replaces_same_value_different_scheme(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="SHARED-IDENTITY",
        connection_target="SHARED-IDENTITY",
        name="Existing custom device",
        pack_id="custom-pack-a",
        platform_id="custom-platform-a",
        identity_scheme="custom_scheme_a",
        identity_scope="host",
        os_version="1.0",
    )

    same_value_different_scheme_response: dict[str, Any] = {
        "candidates": [
            {
                "pack_id": "custom-pack-b",
                "platform_id": "custom-platform-b",
                "identity_scheme": "custom_scheme_b",
                "identity_scope": "host",
                "identity_value": "SHARED-IDENTITY",
                "suggested_name": "New custom device",
                "detected_properties": {
                    "connection_target": "SHARED-IDENTITY",
                    "os_version": "2.0",
                    "device_type": "real_device",
                    "connection_type": "usb",
                },
                "runnable": True,
            }
        ],
    }

    with _patch_pack_devices(same_value_different_scheme_response):
        resp = await client.post(
            f"/api/hosts/{host['id']}/discover/confirm",
            json={"add_identity_values": ["SHARED-IDENTITY"], "remove_identity_values": ["SHARED-IDENTITY"]},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["added"] == ["SHARED-IDENTITY"]
    assert payload["removed"] == ["SHARED-IDENTITY"]

    devices = (await client.get("/api/devices")).json()
    matching = [d for d in devices if d["identity_value"] == "SHARED-IDENTITY"]
    assert [d["identity_scheme"] for d in matching] == ["custom_scheme_b"]


async def test_confirm_discovery_adds_devices(client: AsyncClient) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    with _patch_pack_devices(PACK_DEVICES_RESPONSE):
        resp = await client.post(
            f"/api/hosts/{host['id']}/discover/confirm",
            json={"add_identity_values": ["ABC-123"], "remove_identity_values": []},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "ABC-123" in data["added"]

    devices = (await client.get("/api/devices")).json()
    identities = [d["identity_value"] for d in devices]
    assert "ABC-123" in identities


async def test_confirm_discovery_returns_added_devices_with_readiness(client: AsyncClient) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()
    roku_pack_response: dict[str, Any] = {
        "candidates": [
            {
                "pack_id": "appium-roku",
                "platform_id": "roku_network",
                "identity_scheme": "roku_serial",
                "identity_scope": "global",
                "identity_value": "roku-serial-1",
                "suggested_name": "Living Room Roku",
                "detected_properties": {
                    "connection_target": "192.168.1.50",
                    "os_version": "unknown",
                    "device_type": "real_device",
                    "connection_type": "network",
                    "ip_address": "192.168.1.50",
                },
                "runnable": False,
            }
        ],
    }

    with _patch_pack_devices(roku_pack_response):
        resp = await client.post(
            f"/api/hosts/{host['id']}/discover/confirm",
            json={"add_identity_values": ["roku-serial-1"], "remove_identity_values": []},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["added"] == ["roku-serial-1"]
    assert len(payload["added_devices"]) == 1
    added = payload["added_devices"][0]
    assert added["readiness_state"] == "setup_required"
    assert added["missing_setup_fields"] == ["driver_pack"]
    assert added["lifecycle_policy_summary"]["state"] == "idle"
    assert added["reservation"] is None
    assert added["verified_at"] is None


async def test_confirm_discovery_removes_devices(client: AsyncClient, db_session: AsyncSession) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="TO-REMOVE",
        connection_target="TO-REMOVE",
        name="Removable",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="16.0",
    )

    empty_response: dict[str, Any] = {"candidates": []}
    with _patch_pack_devices(empty_response):
        resp = await client.post(
            f"/api/hosts/{host['id']}/discover/confirm",
            json={"add_identity_values": [], "remove_identity_values": ["TO-REMOVE"]},
        )

    assert resp.status_code == 200
    assert "TO-REMOVE" in resp.json()["removed"]

    devices = (await client.get("/api/devices")).json()
    identities = [d["identity_value"] for d in devices]
    assert "TO-REMOVE" not in identities


async def test_confirm_discovery_updates_existing_device_properties(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()
    existing = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="ABC-123",
        connection_target="ABC-123",
        name="Existing iPhone 15",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.0",
        tags={"owner": "qa"},
        verified=True,
    )

    updated_pack_response: dict[str, Any] = {
        "candidates": [
            {
                "pack_id": "appium-xcuitest",
                "platform_id": "ios",
                "identity_scheme": "apple_udid",
                "identity_scope": "global",
                "identity_value": "ABC-123",
                "suggested_name": "iPhone 15",
                "detected_properties": {
                    "connection_target": "ABC-123-updated",
                    "os_version": "17.4",
                    "manufacturer": "Apple",
                    "model": "iPhone 15",
                },
                "runnable": True,
            }
        ],
    }

    with _patch_pack_devices(updated_pack_response):
        resp = await client.post(
            f"/api/hosts/{host['id']}/discover/confirm",
            json={"add_identity_values": [], "remove_identity_values": []},
        )

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["updated"] == ["ABC-123"]

    refreshed = await client.get(f"/api/devices/{existing.id}")
    assert refreshed.status_code == 200
    body = refreshed.json()
    assert body["os_version"] == "17.4"
    assert body["connection_target"] == "ABC-123"
    assert body["verified_at"] is not None
    assert body["tags"]["owner"] == "qa"
    assert body["manufacturer"] is None
    assert body["model"] is None


async def test_discover_agent_unreachable(client: AsyncClient) -> None:
    host = (await client.post("/api/hosts", json=HOST_PAYLOAD)).json()

    with patch(
        "app.hosts.router.get_pack_devices",
        new=AsyncMock(side_effect=AgentUnreachableError(host["ip"], "Connection refused")),
    ):
        resp = await client.post(f"/api/hosts/{host['id']}/discover")

    assert resp.status_code == 502
