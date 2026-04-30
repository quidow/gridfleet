from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock
from uuid import uuid4

if TYPE_CHECKING:
    import pytest

from app.errors import AgentCallError
from app.models.device import ConnectionType, Device, DeviceType
from app.models.host import Host, HostStatus, OSType
from app.services import bulk_service
from app.services.node_manager_types import NodeManagerError
from app.services.pack_platform_resolver import ResolvedPackPlatform, ResolvedParallelResources


def _device(
    *,
    platform_id: str = "android_mobile",
    pack_id: str = "appium-uiautomator2",
    connection_type: ConnectionType = ConnectionType.usb,
    ip_address: str | None = None,
) -> Device:
    host = Host(
        id=uuid4(),
        hostname="bulk-host",
        ip="10.0.0.10",
        os_type=OSType.linux,
        agent_port=5100,
        status=HostStatus.online,
    )
    return Device(
        id=uuid4(),
        host_id=host.id,
        pack_id=pack_id,
        platform_id=platform_id,
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=str(uuid4()),
        connection_target="target",
        name="Device",
        os_version="14",
        device_type=DeviceType.real_device,
        connection_type=connection_type,
        ip_address=ip_address,
        host=host,
    )


async def test_bulk_start_stop_and_restart_nodes_collect_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    devices = [_device(), _device()]
    db = AsyncMock()
    manager_ok = AsyncMock()
    manager_fail = AsyncMock()
    manager_fail.start_node.side_effect = NodeManagerError("cannot start")
    manager_fail.stop_node.side_effect = RuntimeError("cannot stop")
    manager_fail.restart_node.side_effect = NodeManagerError("cannot restart")

    monkeypatch.setattr("app.services.bulk_service._load_devices", AsyncMock(return_value=devices))
    monkeypatch.setattr(
        "app.services.bulk_service.get_node_manager",
        lambda device: manager_fail if device.id == devices[1].id else manager_ok,
    )
    monkeypatch.setattr("app.services.bulk_service.event_bus.publish", AsyncMock())

    started = await bulk_service.bulk_start_nodes(db, [device.id for device in devices])
    stopped = await bulk_service.bulk_stop_nodes(db, [device.id for device in devices])
    restarted = await bulk_service.bulk_restart_nodes(db, [device.id for device in devices])

    assert started["succeeded"] == 1
    assert stopped["failed"] == 1
    assert restarted["failed"] == 1


async def test_bulk_reconnect_filters_ineligible_devices_and_reports_agent_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eligible_ok = _device(connection_type=ConnectionType.network, ip_address="10.0.0.20")
    eligible_fail = _device(
        platform_id="firetv_real",
        connection_type=ConnectionType.network,
        ip_address="10.0.0.21",
    )
    ineligible = _device(connection_type=ConnectionType.usb)
    db = AsyncMock()

    _reconnect_actions = [{"id": "state"}, {"id": "reconnect"}]
    _resolved = ResolvedPackPlatform(
        pack_id="appium-uiautomator2",
        release="1.0.0",
        platform_id="android_mobile",
        display_name="Android Mobile (Real)",
        automation_name="UiAutomator2",
        appium_platform_name="Android",
        device_types=["real_device"],
        connection_types=["usb", "network"],
        grid_slots=["default"],
        identity_scheme="android_serial",
        identity_scope="host",
        capabilities={},
        default_capabilities={},
        device_fields_schema=[],
        host_fields_schema=[],
        lifecycle_actions=_reconnect_actions,
        health_checks=[],
        connection_behavior={},
        parallel_resources=ResolvedParallelResources(ports=[], derived_data_path=False),
    )

    monkeypatch.setattr(
        "app.services.bulk_service._load_devices",
        AsyncMock(return_value=[eligible_ok, eligible_fail, ineligible]),
    )
    monkeypatch.setattr("app.services.bulk_service.event_bus.publish", AsyncMock())
    monkeypatch.setattr(
        "app.services.bulk_service.resolve_pack_platform",
        AsyncMock(return_value=_resolved),
    )
    monkeypatch.setattr(
        "app.services.bulk_service.pack_device_lifecycle_action",
        AsyncMock(side_effect=[{"success": True}, AgentCallError("10.0.0.10", "boom")]),
    )

    result = await bulk_service.bulk_reconnect(db, [eligible_ok.id, eligible_fail.id, ineligible.id])

    assert result["succeeded"] == 1
    assert result["failed"] == 2
    assert result["errors"][str(ineligible.id)] == "Not a network-connected Android device"


async def test_bulk_delete_and_maintenance_operations_collect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    devices = [_device(), _device()]
    db = AsyncMock()
    monkeypatch.setattr("app.services.bulk_service._load_devices", AsyncMock(return_value=devices))
    monkeypatch.setattr("app.services.bulk_service.event_bus.publish", AsyncMock())
    monkeypatch.setattr(
        "app.services.device_service.delete_device",
        AsyncMock(side_effect=[True, False, RuntimeError("cannot delete")]),
    )
    monkeypatch.setattr(
        "app.services.bulk_service.enter_maintenance",
        AsyncMock(side_effect=[None, RuntimeError("boom")]),
    )
    monkeypatch.setattr(
        "app.services.bulk_service.exit_maintenance",
        AsyncMock(side_effect=[ValueError("bad state"), RuntimeError("boom")]),
    )

    deleted = await bulk_service.bulk_delete(db, [devices[0].id, devices[1].id, uuid4()])
    entered = await bulk_service.bulk_enter_maintenance(db, [device.id for device in devices], drain=True)
    exited = await bulk_service.bulk_exit_maintenance(db, [device.id for device in devices])

    assert deleted["failed"] == 2
    assert entered["failed"] == 1
    assert exited["failed"] == 2
