from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appium_node import AppiumDesiredState, AppiumNode
from app.models.device_event import DeviceEvent, DeviceEventType
from app.models.host import Host, HostStatus, OSType
from app.models.host_resource_sample import HostResourceSample
from app.routers.hosts import _auto_discover, _auto_prepare_host_diagnostics
from app.services import control_plane_state_store
from app.services.agent_circuit_breaker import agent_circuit_breaker
from app.services.host_diagnostics import APPIUM_PROCESSES_NAMESPACE
from tests.helpers import create_device_record

HOST_PAYLOAD = {
    "hostname": "linux-lab-01",
    "ip": "192.168.1.100",
    "os_type": "linux",
    "agent_port": 5100,
}


async def _create_host(client: AsyncClient, **overrides: object) -> dict[str, Any]:
    payload: dict[str, Any] = {**HOST_PAYLOAD, **overrides}
    resp = await client.post("/api/hosts", json=payload)
    assert resp.status_code == 201
    return dict(resp.json())


async def test_create_host(client: AsyncClient) -> None:
    data = await _create_host(client)
    assert data["hostname"] == "linux-lab-01"
    assert data["ip"] == "192.168.1.100"
    assert data["os_type"] == "linux"
    assert data["status"] == "offline"
    assert data["required_agent_version"] == "0.1.0"
    assert data["agent_version_status"] == "unknown"
    assert "id" in data


async def test_create_host_duplicate(client: AsyncClient) -> None:
    await _create_host(client)
    resp = await client.post("/api/hosts", json=HOST_PAYLOAD)
    assert resp.status_code == 409


async def test_list_hosts(client: AsyncClient) -> None:
    await _create_host(client, hostname="host-a")
    await _create_host(client, hostname="host-b", ip="192.168.1.101")

    resp = await client.get("/api/hosts")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


async def test_get_host_with_devices(client: AsyncClient, db_session: AsyncSession) -> None:
    host = await _create_host(client)
    host_id = host["id"]

    await create_device_record(
        db_session,
        host_id=host_id,
        identity_value="dev-001",
        connection_target="dev-001",
        name="Test Device",
        os_version="14",
    )

    resp = await client.get(f"/api/hosts/{host_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["devices"]) == 1
    assert data["devices"][0]["identity_value"] == "dev-001"
    assert data["required_agent_version"] == "0.1.0"
    assert data["agent_version_status"] == "unknown"


async def test_get_host_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/hosts/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


async def test_get_host_diagnostics_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/hosts/00000000-0000-0000-0000-000000000000/diagnostics")
    assert resp.status_code == 404


async def test_get_host_resource_telemetry_returns_bucketed_samples(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host = await _create_host(client)
    host_id = UUID(host["id"])
    base = datetime(2026, 4, 16, 9, 0, tzinfo=UTC)
    db_session.add_all(
        [
            HostResourceSample(
                host_id=host_id,
                recorded_at=base + timedelta(minutes=minute),
                cpu_percent=50.0 + minute,
                memory_used_mb=12000 + minute,
                memory_total_mb=32000,
                disk_used_gb=200.0,
                disk_total_gb=500.0,
                disk_percent=40.0,
            )
            for minute in (0, 1, 2, 20)
        ]
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/hosts/{host_id}/resource-telemetry",
        params={
            "since": base.isoformat(),
            "until": (base + timedelta(minutes=15)).isoformat(),
            "bucket_minutes": 5,
        },
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["bucket_minutes"] == 5
    assert data["window_start"] == "2026-04-16T09:00:00Z"
    assert len(data["samples"]) == 1
    assert data["samples"][0]["timestamp"] == "2026-04-16T09:00:00Z"
    assert data["samples"][0]["cpu_percent"] == pytest.approx(51.0)
    assert data["latest_recorded_at"] == "2026-04-16T09:20:00Z"


async def test_get_host_resource_telemetry_returns_404_for_unknown_host(client: AsyncClient) -> None:
    resp = await client.get("/api/hosts/00000000-0000-0000-0000-000000000000/resource-telemetry")
    assert resp.status_code == 404


async def test_get_host_resource_telemetry_returns_400_for_invalid_window(client: AsyncClient) -> None:
    host = await _create_host(client)
    since = "2026-04-16T10:00:00+00:00"
    until = "2026-04-16T09:00:00+00:00"

    resp = await client.get(
        f"/api/hosts/{host['id']}/resource-telemetry",
        params={"since": since, "until": until},
    )

    assert resp.status_code == 400


async def test_get_host_diagnostics_returns_empty_defaults(client: AsyncClient) -> None:
    host = await _create_host(client)

    resp = await client.get(f"/api/hosts/{host['id']}/diagnostics")
    assert resp.status_code == 200

    data = resp.json()
    assert data["host_id"] == host["id"]
    assert data["circuit_breaker"]["status"] == "closed"
    assert data["circuit_breaker"]["consecutive_failures"] == 0
    assert data["appium_processes"] == {"reported_at": None, "running_nodes": []}
    assert data["recent_recovery_events"] == []


async def test_get_host_diagnostics_returns_enriched_runtime_and_recent_agent_local_history(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host = await _create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="dev-runtime-1",
        connection_target="dev-runtime-1",
        name="Runtime Phone",
        os_version="14",
        operational_state="available",
    )
    node = AppiumNode(
        device_id=device.id,
        port=4723,
        grid_url="http://hub:4444",
        pid=1111,
        desired_state=AppiumDesiredState.running,
        desired_port=4723,
        active_connection_target="",
    )
    db_session.add(node)

    now = datetime.now(UTC)
    db_session.add_all(
        [
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.node_restart,
                details={
                    "source": "agent_local_restart",
                    "process": "grid_relay",
                    "kind": "restart_succeeded",
                    "sequence": 2,
                    "port": 4723,
                    "pid": 2222,
                    "attempt": 1,
                    "delay_sec": 1,
                    "occurred_at": "2026-04-04T10:00:01+00:00",
                    "will_restart": False,
                    "recovered_from": "agent_auto_restart",
                },
                created_at=now,
            ),
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.node_crash,
                details={
                    "source": "agent_local_restart",
                    "kind": "crash_detected",
                    "sequence": 1,
                    "port": 4723,
                    "pid": 1111,
                    "attempt": 1,
                    "delay_sec": 1,
                    "exit_code": 1,
                    "occurred_at": "2026-04-04T10:00:00+00:00",
                    "will_restart": True,
                },
                created_at=now - timedelta(seconds=1),
            ),
            DeviceEvent(
                device_id=device.id,
                event_type=DeviceEventType.node_restart,
                details={"recovered_from": "health_check_failure", "port": 4723},
                created_at=now - timedelta(seconds=2),
            ),
        ]
    )
    await control_plane_state_store.set_value(
        db_session,
        APPIUM_PROCESSES_NAMESPACE,
        host["id"],
        {
            "reported_at": "2026-04-04T10:00:02+00:00",
            "running_nodes": [
                {
                    "port": 4723,
                    "pid": 2222,
                    "connection_target": "dev-runtime-1",
                    "platform_id": "android_mobile",
                },
                {
                    "port": 4999,
                    "pid": 9999,
                    "connection_target": "mystery-runtime",
                    "platform_id": "android_tv",
                },
            ],
        },
    )
    await db_session.commit()

    threshold = agent_circuit_breaker.failure_threshold()
    for _ in range(threshold):
        await agent_circuit_breaker.record_failure(host["ip"], error="timeout")

    resp = await client.get(f"/api/hosts/{host['id']}/diagnostics")
    assert resp.status_code == 200

    data = resp.json()
    assert data["circuit_breaker"]["status"] == "open"
    assert data["circuit_breaker"]["consecutive_failures"] == threshold
    assert data["circuit_breaker"]["retry_after_seconds"] is not None
    assert len(data["appium_processes"]["running_nodes"]) == 2

    managed_node = data["appium_processes"]["running_nodes"][0]
    assert managed_node["managed"] is True
    assert managed_node["device_id"] == str(device.id)
    assert managed_node["device_name"] == "Runtime Phone"
    assert managed_node["node_id"] == str(node.id)
    assert managed_node["node_state"] == "running"
    assert managed_node["platform_id"] == "android_mobile"
    assert "platform" not in managed_node

    unknown_node = data["appium_processes"]["running_nodes"][1]
    assert unknown_node["managed"] is False
    assert unknown_node["device_id"] is None
    assert unknown_node["connection_target"] == "mystery-runtime"
    assert unknown_node["platform_id"] == "android_tv"
    assert "platform" not in unknown_node

    assert [event["kind"] for event in data["recent_recovery_events"]] == [
        "restart_succeeded",
        "crash_detected",
    ]
    assert [event["process"] for event in data["recent_recovery_events"]] == [
        "grid_relay",
        "appium",
    ]
    assert [event["event_type"] for event in data["recent_recovery_events"]] == [
        "node_restart",
        "node_crash",
    ]


async def test_get_host_diagnostics_keeps_last_snapshot_visible_for_offline_host(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host = Host(
        hostname="offline-runtime-host",
        ip="10.0.0.99",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.offline,
    )
    db_session.add(host)
    await db_session.flush()
    await control_plane_state_store.set_value(
        db_session,
        APPIUM_PROCESSES_NAMESPACE,
        str(host.id),
        {
            "reported_at": "2026-04-04T10:30:00+00:00",
            "running_nodes": [
                {"port": 4725, "pid": 5005, "connection_target": "stale-node", "platform_id": "roku_network"}
            ],
        },
    )
    await db_session.commit()

    resp = await client.get(f"/api/hosts/{host.id}/diagnostics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["appium_processes"]["reported_at"] == "2026-04-04T10:30:00Z"
    assert data["appium_processes"]["running_nodes"][0]["managed"] is False
    assert data["appium_processes"]["running_nodes"][0]["connection_target"] == "stale-node"
    assert data["appium_processes"]["running_nodes"][0]["platform_id"] == "roku_network"
    assert "platform" not in data["appium_processes"]["running_nodes"][0]


async def test_get_host_tool_status_proxies_to_agent(client: AsyncClient, db_session: AsyncSession) -> None:
    host = Host(
        hostname="tools-host",
        ip="10.0.0.40",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)

    with patch(
        "app.routers.hosts.get_agent_tool_status",
        new=AsyncMock(
            return_value={
                "appium": "3.3.0",
                "node": "24.14.1",
                "node_provider": "fnm",
                "selenium_jar": "4.41.0",
                "selenium_jar_path": "/opt/gridfleet-agent/selenium-server.jar",
            }
        ),
    ) as status_mock:
        resp = await client.get(f"/api/hosts/{host.id}/tools/status")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["node_provider"] == "fnm"
    assert "appium" not in payload
    assert "selenium_jar" not in payload
    assert "selenium_jar_path" not in payload
    status_mock.assert_awaited_once_with("10.0.0.40", 5100)


async def test_get_host_tool_status_requires_online_host(client: AsyncClient) -> None:
    host = await _create_host(client)

    resp = await client.get(f"/api/hosts/{host['id']}/tools/status")

    assert resp.status_code == 400


async def test_delete_host(client: AsyncClient) -> None:
    host = await _create_host(client)
    resp = await client.delete(f"/api/hosts/{host['id']}")
    assert resp.status_code == 204

    resp = await client.get(f"/api/hosts/{host['id']}")
    assert resp.status_code == 404


async def test_delete_host_with_attached_devices_returns_conflict(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    host = await _create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="dev-002",
        connection_target="dev-002",
        name="Test Device 2",
        os_version="14",
    )
    device_id = str(device.id)

    resp = await client.delete(f"/api/hosts/{host['id']}")
    assert resp.status_code == 409
    assert "devices are still assigned" in resp.json()["error"]["message"]

    resp = await client.get(f"/api/devices/{device_id}")
    assert resp.status_code == 200
    assert resp.json()["host_id"] == host["id"]

    host_resp = await client.get(f"/api/hosts/{host['id']}")
    assert host_resp.status_code == 200


async def test_register_host_returns_version_status_and_schedules_discovery(client: AsyncClient) -> None:
    scheduled: list[tuple[Callable[..., Coroutine[object, object, None]], tuple[object, ...]]] = []

    def capture_schedule(task_fn: Callable[..., Coroutine[object, object, None]], *args: object) -> None:
        scheduled.append((task_fn, args))

    with patch("app.routers.hosts._fire_and_forget", side_effect=capture_schedule):
        resp = await client.post(
            "/api/hosts/register",
            json={
                "hostname": "agent-host",
                "ip": "192.168.1.110",
                "os_type": "linux",
                "agent_port": 5100,
                "agent_version": "0.0.9",
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["required_agent_version"] == "0.1.0"
    assert data["agent_version_status"] == "outdated"
    assert data["recommended_agent_version"] == "0.3.0"
    assert data["agent_update_available"] is True
    host_id = UUID(data["id"])
    assert scheduled == [
        (_auto_discover, (host_id,)),
        (_auto_prepare_host_diagnostics, (host_id,)),
    ]


async def test_hosts_list_and_detail_include_recommended_agent_version(client: AsyncClient) -> None:
    with patch("app.routers.hosts._fire_and_forget"):
        create_resp = await client.post(
            "/api/hosts/register",
            json={
                "hostname": "recommended-version-host",
                "ip": "192.168.1.120",
                "os_type": "linux",
                "agent_port": 5100,
                "agent_version": "0.2.0",
            },
        )

    assert create_resp.status_code == 201
    host_id = create_resp.json()["id"]

    list_resp = await client.get("/api/hosts")
    assert list_resp.status_code == 200
    listed = next(host for host in list_resp.json() if host["id"] == host_id)
    assert listed["recommended_agent_version"] == "0.3.0"
    assert listed["agent_update_available"] is True

    detail_resp = await client.get(f"/api/hosts/{host_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["recommended_agent_version"] == "0.3.0"
    assert detail_resp.json()["agent_update_available"] is True


async def test_agent_update_available_false_when_current(client: AsyncClient) -> None:
    with patch("app.routers.hosts._fire_and_forget"):
        create_resp = await client.post(
            "/api/hosts/register",
            json={
                "hostname": "current-version-host",
                "ip": "192.168.1.121",
                "os_type": "linux",
                "agent_port": 5100,
                "agent_version": "0.3.0",
            },
        )

    assert create_resp.status_code == 201
    assert create_resp.json()["agent_update_available"] is False


async def test_register_host_exposes_missing_prerequisites(client: AsyncClient) -> None:
    with patch("app.routers.hosts._fire_and_forget"):
        resp = await client.post(
            "/api/hosts/register",
            json={
                "hostname": "agent-missing-java",
                "ip": "192.168.1.112",
                "os_type": "linux",
                "agent_port": 5100,
                "agent_version": "0.1.0",
                "capabilities": {
                    "platforms": ["android_mobile", "roku"],
                    "tools": {"appium": "3.0.0"},
                    "missing_prerequisites": ["java"],
                },
            },
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["missing_prerequisites"] == ["java"]
    assert data["capabilities"]["missing_prerequisites"] == ["java"]

    detail_resp = await client.get(f"/api/hosts/{data['id']}")
    assert detail_resp.status_code == 200
    assert detail_resp.json()["missing_prerequisites"] == ["java"]


async def test_approve_host_schedules_discovery_and_diagnostics(client: AsyncClient) -> None:
    scheduled: list[tuple[Callable[..., Coroutine[object, object, None]], tuple[object, ...]]] = []

    def capture_schedule(task_fn: Callable[..., Coroutine[object, object, None]], *args: object) -> None:
        scheduled.append((task_fn, args))

    with patch("app.routers.hosts._fire_and_forget", side_effect=capture_schedule):
        create_resp = await client.put(
            "/api/settings/agent.auto_accept_hosts",
            json={"value": False},
        )
        assert create_resp.status_code in (200, 201)

        register_resp = await client.post(
            "/api/hosts/register",
            json={
                "hostname": "pending-agent",
                "ip": "192.168.1.111",
                "os_type": "linux",
                "agent_port": 5100,
                "agent_version": "0.1.0",
            },
        )
        host_id = register_resp.json()["id"]
        scheduled.clear()

        approve_resp = await client.post(f"/api/hosts/{host_id}/approve")

    assert approve_resp.status_code == 200
    assert approve_resp.json()["agent_version_status"] == "ok"
    host_id = UUID(approve_resp.json()["id"])
    assert scheduled == [
        (_auto_discover, (host_id,)),
        (_auto_prepare_host_diagnostics, (host_id,)),
    ]

    reset_resp = await client.post("/api/settings/reset/agent.auto_accept_hosts")
    assert reset_resp.status_code == 200


async def test_auto_prepare_host_diagnostics_syncs_plugins(db_session: AsyncSession) -> None:
    host = Host(
        hostname="runtime-prepare-host",
        ip="10.0.0.42",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    db_session.add(host)
    await db_session.commit()
    await db_session.refresh(host)
    sync = AsyncMock()
    with (
        patch("app.routers.hosts.host_service.get_host", new=AsyncMock(return_value=host)),
        patch("app.routers.hosts.plugin_service.list_plugins", new=AsyncMock(return_value=[])),
        patch("app.routers.hosts.plugin_service.auto_sync_host_plugins", sync),
    ):
        await _auto_prepare_host_diagnostics(host.id)

    sync.assert_awaited_once_with(host, [])


async def test_hosts_capabilities_reports_terminal_flag(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services.settings_service import settings_service

    # Default is False
    monkeypatch.setitem(settings_service._cache, "agent.enable_web_terminal", False)
    resp = await client.get("/api/hosts/capabilities")
    assert resp.status_code == 200
    assert resp.json()["web_terminal_enabled"] is False

    monkeypatch.setitem(settings_service._cache, "agent.enable_web_terminal", True)
    resp = await client.get("/api/hosts/capabilities")
    assert resp.status_code == 200
    assert resp.json()["web_terminal_enabled"] is True


@pytest.mark.asyncio
async def test_host_discovery_returns_pack_shaped_candidates(
    client: AsyncClient, db_session: AsyncSession, db_host: Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.pack.factories import seed_test_packs

    await seed_test_packs(db_session)
    await db_session.commit()

    async def fake_pack_devices(host_ip: str, host_port: int, **kwargs: object) -> dict[str, object]:
        _ = host_port
        return {
            "candidates": [
                {
                    "pack_id": "appium-uiautomator2",
                    "platform_id": "android_mobile",
                    "identity_scheme": "android_serial",
                    "identity_scope": "host",
                    "identity_value": "emulator-5554",
                    "suggested_name": "Pixel 6",
                    "detected_properties": {"model": "Pixel 6"},
                    "runnable": True,
                }
            ],
        }

    monkeypatch.setattr("app.routers.hosts.get_pack_devices", fake_pack_devices)

    resp = await client.post(f"/api/hosts/{db_host.id}/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert body["new_devices"][0]["pack_id"] == "appium-uiautomator2"
    assert body["new_devices"][0]["platform_label"] == "Android"
