import uuid
from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, HTTPStatusError, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import NodeState
from app.models.device import ConnectionType, Device, DeviceAvailabilityStatus, DeviceType
from app.services.agent_error_codes import AgentErrorCode
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
}
PORT_CONFLICT_DETAIL = (
    "Port 4723 is already in use by another Appium listener; "
    "stop the existing process before starting a new managed node"
)
HOST_PAYLOAD = {
    "hostname": "nodes-host",
    "ip": "10.0.0.40",
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


async def _create_device(db_session: AsyncSession, host_id: str, **overrides: object) -> dict[str, Any]:
    payload = {
        **DEVICE_PAYLOAD,
        "identity_value": f"{DEVICE_PAYLOAD['identity_value']}-{uuid.uuid4().hex[:8]}",
        "connection_target": f"{DEVICE_PAYLOAD['connection_target']}-{uuid.uuid4().hex[:8]}",
        "name": f"{DEVICE_PAYLOAD['name']} {uuid.uuid4().hex[:4]}",
        "host_id": host_id,
        **overrides,
    }
    device = await create_device_record(
        db_session,
        host_id=host_id,
        identity_value=payload["identity_value"],
        connection_target=payload["connection_target"],
        name=payload["name"],
        pack_id=payload["pack_id"],
        platform_id=payload["platform_id"],
        identity_scheme=payload["identity_scheme"],
        identity_scope=payload["identity_scope"],
        os_version=payload["os_version"],
        availability_status=payload.get("availability_status", "offline"),
        device_type=payload.get("device_type", "real_device"),
        connection_type=payload.get("connection_type"),
        ip_address=payload.get("ip_address"),
        verified=payload.get("verified", True),
    )
    return {"id": str(device.id)}


@pytest.fixture
def remote_manager_client() -> Generator[AsyncMock, None, None]:
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get.return_value = _mock_agent_response(
        {"running": True, "port": 4723, "appium_status": {"value": {"ready": True}}}
    )
    with patch("app.services.node_service.httpx.AsyncClient", return_value=mock_client):
        yield mock_client


def _mock_agent_response(json_data: dict[str, Any], status_code: int = 200) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = json_data
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _mock_agent_http_error(detail: str, *, status_code: int = 400) -> MagicMock:
    payload = {
        "detail": {
            "code": AgentErrorCode.PORT_OCCUPIED.value,
            "message": detail,
        }
    }
    response = _mock_agent_response(payload, status_code=status_code)
    response.raise_for_status.side_effect = HTTPStatusError(
        f"{status_code} Server Error",
        request=Request("POST", "http://10.0.0.40:5100/agent/appium/start"),
        response=Response(status_code, json=payload),
    )
    return response


async def test_start_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, availability_status="available")
    device_id = device["id"]
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == NodeState.running.value
    assert data["pid"] == 12345
    assert data["port"] == 4723
    assert data["active_connection_target"] == "emulator-5554"

    # Verify device status updated to available
    device_resp = await client.get(f"/api/devices/{device_id}")
    assert device_resp.json()["availability_status"] == DeviceAvailabilityStatus.available.value


async def test_start_node_already_running(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    await client.post(f"/api/devices/{device_id}/node/start")
    resp = await client.post(f"/api/devices/{device_id}/node/start")
    assert resp.status_code == 400
    assert "already running" in resp.json()["error"]["message"]
    assert remote_manager_client.post.await_count == 1


async def test_start_node_device_not_found(client: AsyncClient) -> None:
    resp = await client.post("/api/devices/00000000-0000-0000-0000-000000000000/node/start")
    assert resp.status_code == 404


async def test_stop_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
    ]

    await client.post(f"/api/devices/{device_id}/node/start")

    resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == NodeState.stopped.value
    assert data["pid"] is None
    assert data["active_connection_target"] is None

    # Verify device status updated to offline
    device_resp = await client.get(f"/api/devices/{device_id}")
    assert device_resp.json()["availability_status"] == DeviceAvailabilityStatus.offline.value


async def test_stop_node_not_running(client: AsyncClient, db_session: AsyncSession, default_host_id: str) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]

    resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert resp.status_code == 400
    assert "No running node" in resp.json()["error"]["message"]


async def test_restart_node(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
        _mock_agent_response({"pid": 12346, "port": 4723, "connection_target": "emulator-5554"}),
    ]

    # Start then restart
    await client.post(f"/api/devices/{device_id}/node/start")
    resp = await client.post(f"/api/devices/{device_id}/node/restart")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == NodeState.running.value


async def test_restart_node_cold_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """Restart on a device with no running node should just start it."""
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )

    resp = await client.post(f"/api/devices/{device_id}/node/restart")
    assert resp.status_code == 200
    assert resp.json()["state"] == NodeState.running.value


async def test_port_allocation_increments(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    """Starting nodes for two devices should allocate different ports."""
    d1 = await _create_device(db_session, default_host_id)
    second = await create_device_record(
        db_session,
        host_id=default_host_id,
        identity_value="emulator-5556",
        connection_target="emulator-5556",
        name="Pixel 8",
        pack_id=DEVICE_PAYLOAD["pack_id"],
        platform_id=DEVICE_PAYLOAD["platform_id"],
        identity_scheme=DEVICE_PAYLOAD["identity_scheme"],
        identity_scope=DEVICE_PAYLOAD["identity_scope"],
        os_version=DEVICE_PAYLOAD["os_version"],
    )
    d2 = {"id": str(second.id)}
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"pid": 12346, "port": 4724, "connection_target": "emulator-5556"}),
    ]

    await client.post(f"/api/devices/{d1['id']}/node/start")
    await client.post(f"/api/devices/{d2['id']}/node/start")

    r1 = await client.get(f"/api/devices/{d1['id']}")
    r2 = await client.get(f"/api/devices/{d2['id']}")

    assert r1.json()["appium_node"]["port"] == 4723
    assert r2.json()["appium_node"]["port"] == 4724


async def test_start_node_agent_failure(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    response = _mock_agent_response({"detail": "startup failed"}, status_code=500)
    response.raise_for_status.side_effect = HTTPStatusError(
        "500 Server Error",
        request=Request("POST", "http://10.0.0.40:5100/appium/start"),
        response=Response(500, json={"detail": "startup failed"}),
    )
    remote_manager_client.post.return_value = response

    device = await _create_device(db_session, default_host_id)
    resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert resp.status_code == 400
    assert "Agent failed to start node: startup failed" in resp.json()["error"]["message"]


async def test_start_node_fails_when_appium_is_not_reachable_after_agent_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, availability_status="available")
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
    ]
    remote_manager_client.get.return_value = _mock_agent_response({"running": False, "port": 4723})

    resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert resp.status_code == 400
    assert "Appium is not reachable" in resp.json()["error"]["message"]

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 200
    assert detail.json()["appium_node"] is None


async def test_start_node_retries_next_port_when_agent_reports_port_conflict(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, availability_status="available")
    remote_manager_client.post.side_effect = [
        _mock_agent_http_error(PORT_CONFLICT_DETAIL),
        _mock_agent_response({"pid": 12345, "port": 4724, "connection_target": "emulator-5554"}),
    ]

    resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert resp.status_code == 200
    assert resp.json()["port"] == 4724

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 200
    assert detail.json()["appium_node"]["port"] == 4724


async def test_restart_node_retries_next_port_when_preferred_port_conflicts(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, availability_status="available")
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
        _mock_agent_http_error(PORT_CONFLICT_DETAIL),
        _mock_agent_response({"pid": 12346, "port": 4724, "connection_target": "emulator-5554"}),
    ]

    start_resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert start_resp.status_code == 200

    resp = await client.post(f"/api/devices/{device['id']}/node/restart")
    assert resp.status_code == 200
    assert resp.json()["port"] == 4724

    detail = await client.get(f"/api/devices/{device['id']}")
    assert detail.status_code == 200
    assert detail.json()["appium_node"]["port"] == 4724


async def test_reserved_device_blocks_node_controls(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id, availability_status="available")
    remote_manager_client.post.return_value = _mock_agent_response(
        {"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}
    )
    run_resp = await client.post(
        "/api/runs",
        json={
            "name": "Reserved Run",
            "requirements": [{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        },
    )
    assert run_resp.status_code == 201

    for action in ("start", "stop", "restart"):
        resp = await client.post(f"/api/devices/{device['id']}/node/{action}")
        assert resp.status_code == 409
        assert "Reserved Run" in resp.json()["error"]["message"]


async def test_maintenance_blocks_start_and_restart_but_not_stop(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
    remote_manager_client: AsyncMock,
) -> None:
    device = await _create_device(db_session, default_host_id)
    device_id = device["id"]
    remote_manager_client.post.side_effect = [
        _mock_agent_response({"pid": 12345, "port": 4723, "connection_target": "emulator-5554"}),
        _mock_agent_response({"stopped": True, "port": 4723}),
    ]

    await client.post(f"/api/devices/{device_id}/node/start")
    maintenance_resp = await client.post(f"/api/devices/{device_id}/maintenance", json={"drain": True})
    assert maintenance_resp.status_code == 200
    assert maintenance_resp.json()["availability_status"] == DeviceAvailabilityStatus.maintenance.value

    for action in ("start", "restart"):
        resp = await client.post(f"/api/devices/{device_id}/node/{action}")
        assert resp.status_code == 409
        assert "maintenance" in resp.json()["error"]["message"]

    stop_resp = await client.post(f"/api/devices/{device_id}/node/stop")
    assert stop_resp.status_code == 200
    assert stop_resp.json()["state"] == NodeState.stopped.value


async def test_unverified_device_blocks_node_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
) -> None:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=f"unverified-{uuid.uuid4()}",
        connection_target=f"unverified-{uuid.uuid4()}",
        name="Needs Verification",
        os_version="14",
        availability_status=DeviceAvailabilityStatus.offline,
        host_id=uuid.UUID(default_host_id),
        verified_at=None,
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)

    resp = await client.post(f"/api/devices/{device.id}/node/start")
    assert resp.status_code == 409
    assert "verification succeeds" in resp.json()["error"]["message"]


async def test_readiness_downgrade_blocks_node_start(
    client: AsyncClient,
    db_session: AsyncSession,
    default_host_id: str,
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

    patch_resp = await client.patch(
        f"/api/devices/{device['id']}",
        json={
            "connection_target": "192.168.1.20:5555",
            "ip_address": "192.168.1.20",
        },
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["verified_at"] is None

    start_resp = await client.post(f"/api/devices/{device['id']}/node/start")
    assert start_resp.status_code == 409
    assert "verification succeeds" in start_resp.json()["error"]["message"]
