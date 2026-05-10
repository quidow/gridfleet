from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumNode, NodeState
from app.models.device import ConnectionType, Device, DeviceType
from app.models.driver_pack import DriverPack, DriverPackPlatform, DriverPackRelease
from app.models.host import Host
from app.observability import BACKGROUND_LOOP_NAMES, build_background_loop_snapshot, set_background_loop_snapshot
from app.schemas.device import DevicePatch, DeviceVerificationCreate
from app.services import device_service
from tests.helpers import create_device_record, create_host
from tests.pack.factories import seed_test_packs

DEVICE_PAYLOAD = {
    "identity_value": "emulator-5554",
    "connection_target": "emulator-5554",
    "name": "Pixel 7 Emulator",
    "pack_id": "appium-uiautomator2",
    "platform_id": "android_mobile",
    "identity_scheme": "android_serial",
    "identity_scope": "host",
    "os_version": "14",
    "tags": {"type": "emulator"},
}

HOST_PAYLOAD = {
    "hostname": "devices-host",
    "ip": "10.0.0.20",
    "os_type": "linux",
    "agent_port": 5100,
}


def test_device_model_declares_scoped_identity_uniqueness() -> None:
    table = Device.__table__
    unique_indexes = {
        (index.name, tuple(column.name for column in index.columns)) for index in table.indexes if index.unique
    }
    assert (
        "uq_devices_identity_scheme_value_global",
        ("identity_scheme", "identity_value"),
    ) in unique_indexes
    assert (
        "uq_devices_host_identity_scheme_value",
        ("host_id", "identity_scheme", "identity_value"),
    ) in unique_indexes
    assert table.c.identity_value.unique is None


@pytest_asyncio.fixture
async def default_host_id(client: AsyncClient, db_session: AsyncSession) -> str:
    await seed_test_packs(db_session)
    host = await create_host(client, **HOST_PAYLOAD)
    return str(host["id"])


def device_payload(host_id: str, **overrides: object) -> dict[str, object]:
    return {**DEVICE_PAYLOAD, "host_id": host_id, **overrides}


def assert_validation_error_for_field(detail: object, field: str) -> None:
    assert isinstance(detail, list)
    assert any(isinstance(entry, dict) and tuple(entry.get("loc", ())) == ("body", field) for entry in detail)


async def _create_device(db_session: AsyncSession, host_id: str, **overrides: object) -> Device:
    payload: dict[str, object] = device_payload(host_id, **overrides)
    if payload.get("device_type") == "emulator" and payload.get("platform_id") == "android_mobile":
        payload["platform_id"] = "android_mobile"
    return await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=str(payload["identity_value"]),
        connection_target=str(payload["connection_target"]) if payload.get("connection_target") is not None else None,
        name=str(payload["name"]),
        pack_id=str(payload["pack_id"]),
        platform_id=str(payload["platform_id"]),
        identity_scheme=str(payload["identity_scheme"]),
        identity_scope=str(payload["identity_scope"]),
        os_version=str(payload["os_version"]),
        tags=payload.get("tags"),
        auto_manage=payload.get("auto_manage", True),
        device_type=payload.get("device_type", "real_device"),
        connection_type=payload.get("connection_type"),
        ip_address=payload.get("ip_address"),
        roku_password=payload.get("roku_password"),
        verified=bool(payload.get("verified", True)),
        operational_state=str(payload.get("operational_state", "offline")),
    )


async def _fake_start_node(db: AsyncSession, device: Device) -> AppiumNode:
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://grid:4444",
        pid=12345,
        state=NodeState.running,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)
    return node


async def _fake_stop_node(db: AsyncSession, device: Device) -> AppiumNode:
    assert device.appium_node is not None
    device.appium_node.state = NodeState.stopped
    await db.commit()
    await db.refresh(device.appium_node)
    return device.appium_node


@pytest.mark.asyncio
async def test_health(client: AsyncClient, db_session: AsyncSession) -> None:
    for loop_name in BACKGROUND_LOOP_NAMES:
        await set_background_loop_snapshot(
            db_session,
            loop_name,
            build_background_loop_snapshot(loop_name, interval_seconds=60.0),
        )
    await db_session.commit()

    resp = await client.get("/api/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_create_device_route_removed(client: AsyncClient, default_host_id: str) -> None:
    resp = await client.post("/api/devices", json=device_payload(default_host_id))
    assert resp.status_code == 405


@pytest.mark.asyncio
async def test_device_persists_manufacturer_and_model_columns(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="serial-column-1",
        connection_target="serial-column-1",
        name="Pixel column",
        manufacturer="Google",
        model="Pixel 8",
    )
    assert device.manufacturer == "Google"
    assert device.model == "Pixel 8"

    resp = await client.get(f"/api/devices/{device.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["manufacturer"] == "Google"
    assert body["model"] == "Pixel 8"


@pytest.mark.asyncio
async def test_list_devices(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_device(db_session, default_host_id)
    await _create_device(
        db_session,
        default_host_id,
        identity_value="ios-001",
        connection_target="ios-001",
        name="iPhone 15",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.4",
    )

    resp = await client.get("/api/devices")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert all("lifecycle_policy_summary" in item for item in data)
    assert all("hardware_health_status" in item for item in data)
    assert all("hardware_telemetry_state" in item for item in data)


@pytest.mark.asyncio
async def test_list_devices_filter_platform(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(db_session, default_host_id)
    await _create_device(
        db_session,
        default_host_id,
        identity_value="ios-001",
        connection_target="ios-001",
        name="iPhone 15",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.4",
    )

    resp = await client.get("/api/devices", params={"platform_id": "ios"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["platform_id"] == "ios"
    assert data[0]["pack_id"] == "appium-xcuitest"


@pytest.mark.asyncio
async def test_list_devices_filters_by_pack_id(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(db_session, default_host_id)
    await _create_device(
        db_session,
        default_host_id,
        identity_value="ios-pack-001",
        connection_target="ios-pack-001",
        name="iPhone Pack",
        pack_id="appium-xcuitest",
        platform_id="ios",
        identity_scheme="apple_udid",
        identity_scope="global",
        os_version="17.4",
    )

    resp = await client.get("/api/devices", params={"pack_id": "appium-uiautomator2"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["pack_id"] == "appium-uiautomator2"
    assert data[0]["platform_id"] == "android_mobile"


@pytest.mark.asyncio
async def test_list_devices_filter_device_type(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(
        db_session,
        default_host_id,
        identity_value="real-device-001",
        connection_target="real-device-001",
        name="Real Device",
        device_type="real_device",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="emulator-001",
        connection_target="emulator-001",
        name="Emulator Device",
        device_type="emulator",
    )

    resp = await client.get("/api/devices", params={"device_type": "emulator"})
    assert resp.status_code == 200
    data = resp.json()
    assert [item["name"] for item in data] == ["Emulator Device"]


@pytest.mark.asyncio
async def test_list_devices_filter_connection_type(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(
        db_session,
        default_host_id,
        identity_value="usb-device-001",
        connection_target="usb-device-001",
        name="USB Device",
        device_type="real_device",
        connection_type="usb",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="network-device-001",
        connection_target="192.168.1.20:5555",
        name="Network Device",
        device_type="real_device",
        connection_type="network",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:virtual-device-001",
        connection_target="Virtual_Device_001",
        name="Virtual Device",
        device_type="emulator",
    )

    resp = await client.get("/api/devices", params={"connection_type": "network"})
    assert resp.status_code == 200
    data = resp.json()
    assert [item["name"] for item in data] == ["Network Device"]

    virtual_resp = await client.get("/api/devices", params={"connection_type": "virtual"})
    assert virtual_resp.status_code == 200
    assert [item["name"] for item in virtual_resp.json()] == ["Virtual Device"]


@pytest.mark.asyncio
async def test_list_devices_filter_os_version(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(
        db_session,
        default_host_id,
        identity_value="android-14",
        connection_target="android-14",
        name="Android 14 Device",
        os_version="14",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="android-15",
        connection_target="android-15",
        name="Android 15 Device",
        os_version="15",
    )

    resp = await client.get("/api/devices", params={"os_version": "15"})
    assert resp.status_code == 200
    data = resp.json()
    assert [item["name"] for item in data] == ["Android 15 Device"]


@pytest.mark.asyncio
async def test_list_devices_filter_search_matches_name_identity_and_target(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    await _create_device(
        db_session,
        default_host_id,
        identity_value="alpha-serial",
        connection_target="alpha-target",
        name="Alpha Device",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="beta-serial",
        connection_target="beta-target",
        name="Beta Device",
    )

    name_resp = await client.get("/api/devices", params={"search": "alpha"})
    assert name_resp.status_code == 200
    assert [item["name"] for item in name_resp.json()] == ["Alpha Device"]

    identity_resp = await client.get("/api/devices", params={"search": "BETA-SERIAL"})
    assert identity_resp.status_code == 200
    assert [item["name"] for item in identity_resp.json()] == ["Beta Device"]

    target_resp = await client.get("/api/devices", params={"search": "beta-target"})
    assert target_resp.status_code == 200
    assert [item["name"] for item in target_resp.json()] == ["Beta Device"]


@pytest.mark.asyncio
async def test_list_devices_filter_tags(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    await _create_device(
        db_session,
        default_host_id,
        identity_value="tagged-qa",
        connection_target="tagged-qa",
        name="Tagged QA",
        tags={"team": "qa", "lane": "smoke"},
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="tagged-dev",
        connection_target="tagged-dev",
        name="Tagged Dev",
        tags={"team": "dev", "lane": "smoke"},
    )

    resp = await client.get("/api/devices", params={"tags.team": "qa", "tags.lane": "smoke"})
    assert resp.status_code == 200
    assert [item["name"] for item in resp.json()] == ["Tagged QA"]


async def test_list_devices_filter_status(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    offline_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="offline-1",
        connection_target="offline-1",
        name="Offline Device",
    )
    online_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="online-1",
        connection_target="online-1",
        name="Online Device",
    )
    offline_id = str(offline_device.id)

    from app.models.device import DeviceOperationalState

    offline_device.operational_state = DeviceOperationalState.offline
    online_device.operational_state = DeviceOperationalState.available
    await db_session.commit()

    resp = await client.get("/api/devices", params={"status": "offline"})
    assert resp.status_code == 200
    data = resp.json()
    assert [item["id"] for item in data] == [offline_id]


@pytest.mark.asyncio
async def test_list_devices_filter_status_busy_overrides_reserved_hold(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    """Device reserved by a run AND running a session must surface as busy, not reserved."""
    busy_reserved = await _create_device(
        db_session,
        default_host_id,
        identity_value="busy-reserved-1",
        connection_target="busy-reserved-1",
        name="Busy Reserved Device",
    )
    plain_reserved = await _create_device(
        db_session,
        default_host_id,
        identity_value="reserved-only-1",
        connection_target="reserved-only-1",
        name="Reserved Only Device",
    )

    from app.models.device import DeviceHold, DeviceOperationalState

    busy_reserved.operational_state = DeviceOperationalState.busy
    busy_reserved.hold = DeviceHold.reserved
    plain_reserved.operational_state = DeviceOperationalState.available
    plain_reserved.hold = DeviceHold.reserved
    await db_session.commit()

    busy_resp = await client.get("/api/devices", params={"status": "busy"})
    assert busy_resp.status_code == 200
    busy_ids = {item["id"] for item in busy_resp.json()}
    assert str(busy_reserved.id) in busy_ids
    assert str(plain_reserved.id) not in busy_ids

    reserved_resp = await client.get("/api/devices", params={"status": "reserved"})
    assert reserved_resp.status_code == 200
    reserved_ids = {item["id"] for item in reserved_resp.json()}
    assert str(plain_reserved.id) in reserved_ids
    assert str(busy_reserved.id) not in reserved_ids


@pytest.mark.asyncio
async def test_get_device(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    resp = await client.get(f"/api/devices/{device_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["identity_value"] == "emulator-5554"
    assert data["pack_id"] == "appium-uiautomator2"
    assert data["platform_id"] == "android_mobile"
    assert data["platform_label"] == "Android"
    assert "blocked_reason" in data
    assert data["identity_scheme"] == "android_serial"
    assert data["identity_scope"] == "host"
    assert data["appium_node"] is None
    assert data["sessions"] == []
    assert data["lifecycle_policy_summary"]["label"] == "Idle"
    assert data["emulator_state"] is None
    assert data["hardware_health_status"] == "unknown"
    assert data["hardware_telemetry_state"] == "unknown"
    assert data["battery_level_percent"] is None


@pytest.mark.asyncio
async def test_get_device_includes_hardware_telemetry_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="telemetry-1",
        connection_target="telemetry-1",
    )
    device.battery_level_percent = 76
    device.battery_temperature_c = 37.4
    device.charging_state = "charging"
    device.hardware_health_status = "healthy"
    device.hardware_telemetry_support_status = "supported"

    device.hardware_telemetry_reported_at = datetime.now(UTC)
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["battery_level_percent"] == 76
    assert data["battery_temperature_c"] == 37.4
    assert data["charging_state"] == "charging"
    assert data["hardware_health_status"] == "healthy"
    assert data["hardware_telemetry_state"] == "fresh"


@pytest.mark.asyncio
async def test_list_devices_filter_hardware_health_status(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    warning_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="warning-telemetry",
        connection_target="warning-telemetry",
        name="Warning Telemetry",
    )
    healthy_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="healthy-telemetry",
        connection_target="healthy-telemetry",
        name="Healthy Telemetry",
    )
    warning_device.hardware_health_status = "warning"
    healthy_device.hardware_health_status = "healthy"
    await db_session.commit()

    resp = await client.get("/api/devices", params={"hardware_health_status": "warning"})
    assert resp.status_code == 200
    assert [item["name"] for item in resp.json()] == ["Warning Telemetry"]


@pytest.mark.asyncio
async def test_list_devices_filter_hardware_telemetry_state(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    stale_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="stale-telemetry",
        connection_target="stale-telemetry",
        name="Stale Telemetry",
    )
    unsupported_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="unsupported-telemetry",
        connection_target="unsupported-telemetry",
        name="Unsupported Telemetry",
    )
    stale_device.hardware_telemetry_support_status = "supported"
    stale_device.hardware_telemetry_reported_at = datetime.now(UTC) - timedelta(hours=1)
    unsupported_device.hardware_telemetry_support_status = "unsupported"
    unsupported_device.hardware_telemetry_reported_at = datetime.now(UTC)
    await db_session.commit()

    from app.services.settings_service import settings_service

    settings_service._cache["general.hardware_telemetry_stale_timeout_sec"] = 60

    stale_resp = await client.get("/api/devices", params={"hardware_telemetry_state": "stale"})
    assert stale_resp.status_code == 200
    assert [item["name"] for item in stale_resp.json()] == ["Stale Telemetry"]

    unsupported_resp = await client.get("/api/devices", params={"hardware_telemetry_state": "unsupported"})
    assert unsupported_resp.status_code == 200
    assert [item["name"] for item in unsupported_resp.json()] == ["Unsupported Telemetry"]


@pytest.mark.asyncio
async def test_list_devices_filter_needs_attention(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    verified_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="NEED-ATTN-OK",
        connection_target="NEED-ATTN-OK",
        name="Verified Device",
        operational_state="available",
        verified=True,
    )
    unverified_device = await _create_device(
        db_session,
        default_host_id,
        identity_value="NEED-ATTN-BAD",
        connection_target="NEED-ATTN-BAD",
        name="Unverified Device",
        operational_state="offline",
        verified=False,
    )
    _ = verified_device
    _ = unverified_device

    resp_true = await client.get("/api/devices", params={"needs_attention": "true"})
    assert resp_true.status_code == 200
    items_true = resp_true.json()
    assert all(d["needs_attention"] is True for d in items_true)
    assert any(d["identity_value"] == "NEED-ATTN-BAD" for d in items_true)
    assert not any(d["identity_value"] == "NEED-ATTN-OK" for d in items_true)

    resp_false = await client.get("/api/devices", params={"needs_attention": "false"})
    assert resp_false.status_code == 200
    items_false = resp_false.json()
    assert all(d["needs_attention"] is False for d in items_false)
    assert any(d["identity_value"] == "NEED-ATTN-OK" for d in items_false)
    assert not any(d["identity_value"] == "NEED-ATTN-BAD" for d in items_false)


@pytest.mark.asyncio
async def test_device_detail_surfaces_emulator_state(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd-pixel-6",
        connection_target="Pixel_6",
        name="Pixel 6 Emulator",
        device_type="emulator",
    )
    device_id = str(device.id)

    from app.services import device_health

    await device_health.update_emulator_state(db_session, device, "running")
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["connection_type"] == "virtual"
    assert data["emulator_state"] == "running"


@pytest.mark.asyncio
async def test_device_create_payload_preserves_explicit_unified_platform_lane(
    db_session: AsyncSession, default_host_id: str
) -> None:
    payload = await device_service.prepare_device_create_payload(
        db_session,
        DeviceVerificationCreate(
            host_id=default_host_id,
            name="Pixel 6 Emulator",
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value="avd:Pixel_6",
            connection_target="Pixel_6",
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        ),
    )

    assert payload["device_type"] == DeviceType.real_device
    assert payload["connection_type"] == ConnectionType.usb


@pytest.mark.asyncio
async def test_get_device_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_refresh_device_properties_returns_serialized_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    async def fake_get_pack_device_properties(
        host: str, agent_port: int, connection_target: str, pack_id: str, **kwargs: object
    ) -> dict[str, object]:
        return {
            "detected_properties": {
                "os_version": "15",
                "connection_target": "emulator-5554",
                "device_type": "emulator",
                "connection_type": "virtual",
            }
        }

    with patch("app.routers.devices_control.get_pack_device_properties", side_effect=fake_get_pack_device_properties):
        resp = await client.post(f"/api/devices/{device_id}/refresh")

    assert resp.status_code == 200
    data = resp.json()
    assert data["os_version"] == "15"
    assert data["lifecycle_policy_summary"]["state"] == "idle"


@pytest.mark.asyncio
async def test_update_device(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    resp = await client.patch(
        f"/api/devices/{device_id}",
        json={"name": "Updated Name", "tags": {"owner": "qa"}, "auto_manage": False},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Updated Name"
    assert data["tags"]["owner"] == "qa"
    assert data["auto_manage"] is False
    assert data["lifecycle_policy_summary"]["state"] == "idle"


@pytest.mark.asyncio
async def test_update_device_acquires_row_lock(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)
    real_lock = device_service.device_locking.lock_device
    spy = AsyncMock(side_effect=real_lock)

    with patch("app.services.device_service.device_locking.lock_device", spy):
        resp = await client.patch(f"/api/devices/{device_id}", json={"name": "Locked Update"})

    assert resp.status_code == 200
    spy.assert_awaited_once()
    args, _ = spy.await_args
    assert str(args[1]) == device_id


@pytest.mark.asyncio
async def test_update_device_returns_none_when_device_missing(client: AsyncClient, db_session: AsyncSession) -> None:
    import uuid

    missing_id = uuid.uuid4()
    result = await device_service.update_device(
        db_session,
        missing_id,
        DevicePatch(),
        enforce_patch_contract=False,
    )
    assert result is None


@pytest.mark.asyncio
async def test_patch_rejects_immutable_fields(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    second_host_resp = await client.post(
        "/api/hosts",
        json={**HOST_PAYLOAD, "hostname": "devices-host-2", "ip": "10.0.0.21"},
    )
    assert second_host_resp.status_code == 201
    second_host_id = second_host_resp.json()["id"]

    cases = [
        ("identity_kind", "apple_udid"),
        ("identity_value", "new-identity"),
        ("platform", "ios"),
        ("device_type", "emulator"),
        ("os_version", "15"),
        ("status", "available"),
        ("host_id", second_host_id),
        ("connection_type", "network"),
    ]
    for field, value in cases:
        resp = await client.patch(f"/api/devices/{device_id}", json={field: value})
        assert resp.status_code == 422
        assert_validation_error_for_field(resp.json()["error"]["details"], field)


@pytest.mark.asyncio
async def test_non_readiness_edit_preserves_verified_at(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)
    original_verified_at = device.verified_at

    resp = await client.patch(f"/api/devices/{device_id}", json={"name": "Renamed Device"})

    assert resp.status_code == 200
    assert original_verified_at is not None
    assert resp.json()["verified_at"] == original_verified_at.isoformat().replace("+00:00", "Z")
    assert resp.json()["readiness_state"] == "verified"


@pytest.mark.asyncio
async def test_readiness_edit_clears_verified_at(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="android-network-stable",
        connection_target="192.168.1.10:5555",
        device_type="real_device",
        connection_type="network",
        ip_address="192.168.1.10",
    )
    device_id = str(device.id)
    assert device.verified_at is not None

    resp = await client.patch(
        f"/api/devices/{device_id}",
        json={
            "connection_target": "192.168.1.20:5555",
            "ip_address": "192.168.1.20",
        },
    )

    assert resp.status_code == 200
    assert resp.json()["verified_at"] is None
    assert resp.json()["readiness_state"] == "verification_required"


@pytest.mark.asyncio
async def test_device_detail_surfaces_lifecycle_policy_summary(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)
    device.lifecycle_policy_state = {
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
    await db_session.commit()

    resp = await client.get(f"/api/devices/{device_id}")

    assert resp.status_code == 200
    data = resp.json()
    assert data["lifecycle_policy_summary"]["state"] == "deferred_stop"
    assert data["lifecycle_policy_summary"]["detail"] == "ADB not responsive"


@pytest.mark.asyncio
async def test_delete_device(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    resp = await client.delete(f"/api/devices/{device_id}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/devices/{device_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_device_not_found(client: AsyncClient) -> None:
    resp = await client.delete("/api/devices/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_android_connection_preserves_canonical_identity(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="emulator-5554",
        name="Pixel",
        device_type="real_device",
        connection_type="network",
        connection_target="192.168.1.10:5555",
        ip_address="192.168.1.10",
    )
    device_id = str(device.id)

    resp = await client.patch(
        f"/api/devices/{device_id}",
        json={
            "connection_target": "192.168.1.20:5555",
            "ip_address": "192.168.1.20",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["identity_value"] == "emulator-5554"
    assert resp.json()["connection_target"] == "192.168.1.20:5555"
    assert resp.json()["connection_type"] == "network"

    config_resp = await client.get(f"/api/devices/{device_id}/config", params={"reveal": True})
    assert config_resp.status_code == 200
    assert "canonical_identity" not in config_resp.json()


@pytest.mark.asyncio
async def test_patch_rejects_endpoint_edit_for_non_network_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    resp = await client.patch(
        f"/api/devices/{device_id}",
        json={"connection_target": "192.168.1.20:5555", "ip_address": "192.168.1.20"},
    )

    assert resp.status_code == 422
    assert "connection target edits" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_patch_allows_connection_target_edit_for_virtual_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        name="Pixel 6 Emulator",
        device_type="emulator",
    )

    resp = await client.patch(f"/api/devices/{device.id}", json={"connection_target": "Pixel_6_Updated"})

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["connection_target"] == "Pixel_6_Updated"
    assert payload["connection_type"] == "virtual"
    assert payload["ip_address"] is None
    assert payload["verified_at"] is None


@pytest.mark.asyncio
async def test_patch_rejects_ip_address_edit_for_virtual_device(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        name="Pixel 6 Emulator",
        device_type="emulator",
    )

    resp = await client.patch(f"/api/devices/{device.id}", json={"ip_address": "192.168.1.20"})

    assert resp.status_code == 422
    assert "IP address edits" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_manual_session_test_endpoint(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    with patch(
        "app.routers.devices.session_viability.run_session_viability_probe",
        new_callable=AsyncMock,
        return_value={
            "status": "passed",
            "last_attempted_at": "2026-03-30T10:00:00+00:00",
            "last_succeeded_at": "2026-03-30T10:00:00+00:00",
            "error": None,
            "checked_by": "manual",
        },
    ):
        resp = await client.post(f"/api/devices/{device_id}/session-test")

    assert resp.status_code == 200
    assert resp.json()["status"] == "passed"


@pytest.mark.asyncio
async def test_enter_device_maintenance_stops_running_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    with (
        patch("app.routers.nodes.start_managed_node", new=_fake_start_node),
        patch("app.services.maintenance_service.stop_node", new=_fake_stop_node),
    ):
        start_resp = await client.post(f"/api/devices/{device_id}/node/start")
        assert start_resp.status_code == 200

        maintenance_resp = await client.post(f"/api/devices/{device_id}/maintenance", json={})

    assert maintenance_resp.status_code == 200
    assert maintenance_resp.json()["hold"] == "maintenance"

    device_resp = await client.get(f"/api/devices/{device_id}")
    assert device_resp.status_code == 200
    assert device_resp.json()["hold"] == "maintenance"
    assert device_resp.json()["appium_node"]["state"] == "stopped"


@pytest.mark.asyncio
async def test_exit_device_maintenance(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    enter_resp = await client.post(f"/api/devices/{device_id}/maintenance", json={})
    assert enter_resp.status_code == 200

    exit_resp = await client.post(f"/api/devices/{device_id}/maintenance/exit")
    assert exit_resp.status_code == 200
    assert exit_resp.json()["operational_state"] == "offline"


@pytest.mark.asyncio
async def test_exit_device_maintenance_requires_maintenance_status(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = str(device.id)

    exit_resp = await client.post(f"/api/devices/{device_id}/maintenance/exit")
    assert exit_resp.status_code == 409
    assert "not in maintenance" in exit_resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_device_health_is_unhealthy_when_session_check_failed(client: AsyncClient) -> None:
    fake_device = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000123",
        host_id="00000000-0000-0000-0000-000000000010",
        identity_value="health-001",
        connection_target="health-001",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
        connection_type=SimpleNamespace(value="usb"),
        ip_address=None,
        host=SimpleNamespace(ip="10.0.0.10", agent_port=5100),
        appium_node=SimpleNamespace(state=SimpleNamespace(value="running"), port=4723),
    )

    with (
        patch("app.routers.devices.device_service.get_device", new_callable=AsyncMock, return_value=fake_device),
        patch(
            "app.routers.devices.session_viability.get_session_viability",
            new_callable=AsyncMock,
            return_value={
                "status": "failed",
                "last_attempted_at": "2026-03-30T10:00:00+00:00",
                "last_succeeded_at": None,
                "error": "Session startup failed",
                "checked_by": "scheduled",
            },
        ),
        patch(
            "app.routers.devices.lifecycle_policy.build_lifecycle_policy",
            new_callable=AsyncMock,
            return_value={
                "last_failure_source": "session_viability",
                "last_failure_reason": "Session startup failed",
                "last_action": "auto_stopped",
                "last_action_at": "2026-03-30T10:05:00+00:00",
                "stop_pending": False,
                "stop_pending_reason": None,
                "stop_pending_since": None,
                "excluded_from_run": False,
                "excluded_run_id": None,
                "excluded_run_name": None,
                "excluded_at": None,
                "will_auto_rejoin_run": False,
                "recovery_suppressed_reason": None,
                "backoff_until": None,
                "recovery_state": "eligible",
            },
        ),
        patch("app.routers.devices.httpx.AsyncClient") as mock_client_cls,
    ):
        mock_client = mock_client_cls.return_value
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {"healthy": True, "adb_connected": {"connected": True}}
        mock_client.get = AsyncMock(return_value=mock_response)
        resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000123/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["device_checks"]["healthy"] is True
    assert data["session_viability"]["status"] == "failed"
    assert data["lifecycle_policy"]["last_failure_source"] == "session_viability"
    assert data["healthy"] is False


@pytest.mark.asyncio
async def test_device_health_is_unhealthy_when_runtime_node_is_not_reachable(client: AsyncClient) -> None:
    fake_device = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000126",
        host_id="00000000-0000-0000-0000-000000000010",
        identity_value="health-node-001",
        connection_target="health-node-001",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="real_device"),
        connection_type=SimpleNamespace(value="usb"),
        ip_address=None,
        host=SimpleNamespace(ip="10.0.0.10", agent_port=5100),
        appium_node=SimpleNamespace(state=SimpleNamespace(value="running"), port=4723),
    )

    with (
        patch(
            "app.routers.devices_control.get_device_or_404",
            new_callable=AsyncMock,
            return_value=fake_device,
        ),
        patch(
            "app.routers.devices_control.fetch_pack_device_health",
            new_callable=AsyncMock,
            return_value={"healthy": True, "adb_connected": {"connected": True}},
        ),
        patch(
            "app.routers.devices_control.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": False, "port": 4723},
        ),
        patch(
            "app.routers.devices_control.session_viability.get_session_viability",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.devices_control.lifecycle_policy.build_lifecycle_policy",
            new_callable=AsyncMock,
            return_value={
                "last_failure_source": None,
                "last_failure_reason": None,
                "last_action": None,
                "last_action_at": None,
                "stop_pending": False,
                "stop_pending_reason": None,
                "stop_pending_since": None,
                "excluded_from_run": False,
                "excluded_run_id": None,
                "excluded_run_name": None,
                "excluded_at": None,
                "will_auto_rejoin_run": False,
                "recovery_suppressed_reason": None,
                "backoff_until": None,
                "recovery_state": "idle",
            },
        ),
    ):
        resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000126/health")

    assert resp.status_code == 200
    data = resp.json()
    assert data["device_checks"]["healthy"] is True
    assert data["node"]["running"] is False
    assert data["node"]["state"] == "error"
    assert data["healthy"] is False


@pytest.mark.asyncio
async def test_device_health_passes_pack_context_for_virtual_devices(client: AsyncClient) -> None:
    fake_device = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000127",
        host_id="00000000-0000-0000-0000-000000000010",
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        device_type=SimpleNamespace(value="emulator"),
        connection_type=SimpleNamespace(value="virtual"),
        ip_address=None,
        host=SimpleNamespace(ip="10.0.0.10", agent_port=5100),
        appium_node=SimpleNamespace(state=SimpleNamespace(value="running"), port=4723),
    )
    health_mock = AsyncMock(return_value={"healthy": True, "adb_connected": {"connected": True}})

    with (
        patch(
            "app.routers.devices_control.get_device_or_404",
            new_callable=AsyncMock,
            return_value=fake_device,
        ),
        patch("app.routers.devices_control.fetch_pack_device_health", health_mock),
        patch(
            "app.routers.devices_control.fetch_appium_status",
            new_callable=AsyncMock,
            return_value={"running": True, "port": 4723},
        ),
        patch(
            "app.routers.devices_control.session_viability.get_session_viability",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.routers.devices_control.lifecycle_policy.build_lifecycle_policy",
            new_callable=AsyncMock,
            return_value={
                "last_failure_source": None,
                "last_failure_reason": None,
                "last_action": None,
                "last_action_at": None,
                "stop_pending": False,
                "stop_pending_reason": None,
                "stop_pending_since": None,
                "excluded_from_run": False,
                "excluded_run_id": None,
                "excluded_run_name": None,
                "excluded_at": None,
                "will_auto_rejoin_run": False,
                "recovery_suppressed_reason": None,
                "backoff_until": None,
                "recovery_state": "idle",
            },
        ),
    ):
        resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000127/health")

    assert resp.status_code == 200
    assert resp.json()["healthy"] is True
    health_mock.assert_awaited_once()
    _, _, connection_target = health_mock.await_args.args[:3]
    assert connection_target == "Pixel_6"
    assert health_mock.await_args.kwargs["device_type"] == "emulator"
    assert health_mock.await_args.kwargs["connection_type"] == "virtual"


@pytest.mark.asyncio
async def test_device_health_fails_fast_for_hostless_control_plane_state(client: AsyncClient) -> None:
    fake_device = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000124",
        host_id=None,
        host=None,
    )

    with patch(
        "app.routers.devices_control.get_device_or_404",
        new_callable=AsyncMock,
        return_value=fake_device,
    ):
        resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000124/health")

    assert resp.status_code == 400
    assert "has no host assigned" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_device_logs_fail_fast_for_hostless_control_plane_state(client: AsyncClient) -> None:
    fake_device = SimpleNamespace(
        id="00000000-0000-0000-0000-000000000125",
        host_id=None,
        host=None,
    )

    with patch(
        "app.routers.devices_control.get_device_or_404",
        new_callable=AsyncMock,
        return_value=fake_device,
    ):
        resp = await client.get("/api/devices/00000000-0000-0000-0000-000000000125/logs")

    assert resp.status_code == 400
    assert "has no host assigned" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Driver-pack lifecycle endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_lifecycle_action_proxies_to_pack_agent(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        device_type="emulator",
    )

    with patch(
        "app.routers.devices_control.pack_device_lifecycle_action",
        new_callable=AsyncMock,
        return_value={"success": True, "state": "running"},
    ) as mock_lifecycle:
        resp = await client.post(f"/api/devices/{device.id}/lifecycle/boot", json={"headless": True})

    assert resp.status_code == 200
    assert resp.json()["state"] == "running"
    mock_lifecycle.assert_awaited_once()
    _, kwargs = mock_lifecycle.call_args
    assert kwargs["pack_id"] == "appium-uiautomator2"
    assert kwargs["platform_id"] == "android_mobile"
    assert kwargs["action"] == "boot"
    assert kwargs["args"] == {"headless": True}


@pytest.mark.asyncio
async def test_device_lifecycle_state_updates_emulator_state(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        device_type="emulator",
    )

    with patch(
        "app.routers.devices_control.pack_device_lifecycle_action",
        new_callable=AsyncMock,
        return_value={"success": True, "state": "running"},
    ):
        resp = await client.post(f"/api/devices/{device.id}/lifecycle/state")

    assert resp.status_code == 200
    detail_resp = await client.get(f"/api/devices/{device.id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["emulator_state"] == "running"


@pytest.mark.asyncio
async def test_device_lifecycle_action_rejects_unsupported_action(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="real-android-001",
        connection_target="real-android-001",
    )

    resp = await client.post(f"/api/devices/{device.id}/lifecycle/boot")

    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "Lifecycle action boot is not supported for this device platform"


@pytest.mark.asyncio
async def test_deleted_emulator_and_simulator_lifecycle_routes_return_404(
    client: AsyncClient, db_session: AsyncSession, default_host_id: str
) -> None:
    device = await _create_device(
        db_session,
        default_host_id,
        identity_value="avd:Pixel_6",
        connection_target="Pixel_6",
        device_type="emulator",
    )

    assert (await client.post(f"/api/devices/{device.id}/emulator/launch")).status_code == 404
    assert (await client.post(f"/api/devices/{device.id}/emulator/shutdown")).status_code == 404
    assert (await client.post(f"/api/devices/{device.id}/simulator/boot")).status_code == 404
    assert (await client.post(f"/api/devices/{device.id}/simulator/shutdown")).status_code == 404


@pytest.mark.asyncio
async def test_list_devices_paginated(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    for i in range(5):
        await _create_device(
            db_session,
            default_host_id,
            identity_value=f"dev-{i}",
            connection_target=f"dev-{i}",
            name=f"Device {i}",
        )

    resp = await client.get("/api/devices", params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    data = resp.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert data["limit"] == 2
    assert data["offset"] == 0


@pytest.mark.asyncio
async def test_list_devices_paginated_second_page(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    for i in range(5):
        await _create_device(
            db_session,
            default_host_id,
            identity_value=f"dev-{i}",
            connection_target=f"dev-{i}",
            name=f"Device {i}",
        )

    resp = await client.get("/api/devices", params={"limit": 2, "offset": 4})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    assert data["total"] == 5


@pytest.mark.asyncio
async def test_list_devices_unpaginated_still_works(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_device(db_session, default_host_id)

    resp = await client.get("/api/devices")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)


@pytest.mark.asyncio
async def test_list_devices_supports_sort_by_name(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    await _create_device(
        db_session,
        default_host_id,
        identity_value="s-c",
        connection_target="s-c",
        name="Charlie",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="s-a",
        connection_target="s-a",
        name="Alpha",
    )
    await _create_device(
        db_session,
        default_host_id,
        identity_value="s-b",
        connection_target="s-b",
        name="Bravo",
    )

    resp = await client.get(
        "/api/devices",
        params={"limit": 2, "offset": 0, "sort_by": "name", "sort_dir": "asc"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [d["name"] for d in body["items"]] == ["Alpha", "Bravo"]
    assert body["total"] >= 3

    resp2 = await client.get(
        "/api/devices",
        params={"limit": 2, "offset": 2, "sort_by": "name", "sort_dir": "asc"},
    )
    assert resp2.status_code == 200
    assert [d["name"] for d in resp2.json()["items"]] == ["Charlie"]


@pytest.mark.asyncio
async def test_needs_attention_filter_includes_unhealthy_devices(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    from app.services import device_health

    await _create_device(
        db_session,
        default_host_id,
        identity_value="ok-1",
        connection_target="ok-1",
        name="OK Device",
        operational_state="available",
        verified=True,
    )
    unhealthy = await _create_device(
        db_session,
        default_host_id,
        identity_value="bad-1",
        connection_target="bad-1",
        name="Bad Device",
        operational_state="available",
        verified=True,
    )

    await device_health.update_device_checks(db_session, unhealthy, healthy=False, summary="Disconnected")
    await db_session.commit()

    resp = await client.get("/api/devices", params={"needs_attention": "true"})
    assert resp.status_code == 200
    body = resp.json()
    names = [d["name"] for d in body]
    assert "Bad Device" in names
    assert "OK Device" not in names


# ---------------------------------------------------------------------------
# Task 9: WDA cap synthesis from manifest device fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tvos_device_wda_caps_come_from_required_device_fields(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_test_packs(db_session)
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="UDID-1",
        name="tvos-1",
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        ip_address="10.0.0.42",
        device_config={
            "wda_base_url": "http://10.0.0.42",
            "use_preinstalled_wda": True,
            "updated_wda_bundle_id": "com.test.WebDriverAgentRunner",
        },
    )
    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    caps = resp.json()
    assert caps["appium:platformVersion"] == "14"
    assert caps["appium:wdaBaseUrl"] == "http://10.0.0.42"
    assert caps["appium:usePreinstalledWDA"] is True
    assert caps["appium:updatedWDABundleId"] == "com.test.WebDriverAgentRunner"


@pytest.mark.asyncio
async def test_caller_supplied_use_preinstalled_wda_overrides_manifest_default(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await seed_test_packs(db_session)
    host = await create_host(client, **HOST_PAYLOAD)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="UDID-2",
        name="tvos-2",
        pack_id="appium-xcuitest",
        platform_id="tvos",
        identity_scheme="apple_udid",
        identity_scope="global",
        ip_address="10.0.0.43",
    )
    device.device_config = {"wda_base_url": "http://10.0.0.43", "use_preinstalled_wda": False}
    await db_session.commit()
    resp = await client.get(f"/api/devices/{device.id}/capabilities")
    assert resp.status_code == 200
    caps = resp.json()
    assert caps["appium:usePreinstalledWDA"] is False


async def test_roku_device_without_pack_surfaces_pack_unavailable(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    await seed_test_packs(db_session)
    device = await create_device_record(
        db_session,
        host_id=db_host.id,
        identity_value="ROKU-1",
        name="r1",
        pack_id="appium-roku",
        platform_id="roku_network",
        identity_scheme="roku_serial",
        identity_scope="global",
        device_type="real_device",
    )
    await db_session.commit()
    resp = await client.get(f"/api/devices/{device.id}")
    assert resp.status_code == 200
    assert resp.json()["blocked_reason"] == "pack_unavailable"


@pytest.mark.asyncio
async def test_device_detail_uses_catalog_readiness_for_local_pack(
    client: AsyncClient, db_session: AsyncSession, db_host: Host
) -> None:
    pack = DriverPack(
        id="local/test-driver",
        origin="uploaded",
        display_name="Local Test Driver",
        maintainer="qa",
        license="",
        state="enabled",
    )
    db_session.add(pack)
    await db_session.flush()
    release = DriverPackRelease(pack_id=pack.id, release="2026.04.0", manifest_json={})
    db_session.add(release)
    await db_session.flush()
    db_session.add(
        DriverPackPlatform(
            pack_release_id=release.id,
            manifest_platform_id="test_network",
            display_name="Test Network",
            automation_name="TestAutomation",
            appium_platform_name="TestOS",
            device_types=["real_device"],
            connection_types=["network"],
            grid_slots=["native"],
            data={
                "identity": {"scheme": "test_id", "scope": "host"},
                "device_fields_schema": [
                    {
                        "id": "api_token",
                        "label": "API token",
                        "type": "string",
                        "required_for_session": True,
                        "sensitive": True,
                    }
                ],
            },
        )
    )
    device = Device(
        pack_id=pack.id,
        platform_id="test_network",
        identity_scheme="test_id",
        identity_scope="host",
        identity_value="device-1",
        connection_target="device-1",
        name="Local Test Device",
        os_version="1.0",
        host_id=db_host.id,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.network,
        ip_address="10.0.0.10",
        device_config={},
    )
    db_session.add(device)
    await db_session.commit()

    response = await client.get(f"/api/devices/{device.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["readiness_state"] == "setup_required"
    assert body["missing_setup_fields"] == ["api_token"]


def test_backend_sanitize_log_value_strips_control_characters() -> None:
    from app.observability import sanitize_log_value

    assert sanitize_log_value("device-1\r\ninjected=true") == "device-1\\r\\ninjected=true"
