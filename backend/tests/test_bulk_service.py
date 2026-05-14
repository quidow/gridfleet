from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

from app.appium_nodes.exceptions import NodeManagerError
from app.core.errors import AgentCallError
from app.devices.models import ConnectionType, Device, DeviceHold, DeviceOperationalState, DeviceType
from app.devices.services import bulk as bulk_service
from app.hosts.models import Host, HostStatus, OSType
from app.jobs.kinds import JOB_KIND_DEVICE_RECOVERY
from app.jobs.models import Job
from app.packs.services.platform_resolver import ResolvedPackPlatform, ResolvedParallelResources
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


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


async def test_bulk_start_stop_and_restart_nodes_collect_errors(
    monkeypatch: pytest.MonkeyPatch,
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,
            name="bulk-manager-ok",
            operational_state=DeviceOperationalState.available,
            verified=True,
        ),
        await create_device(
            db_session,
            host_id=db_host.id,
            name="bulk-manager-fail",
            operational_state=DeviceOperationalState.available,
            verified=True,
        ),
    ]

    async def fake_start_node(_db: AsyncSession, device: Device, caller: str) -> object:
        if device.id == devices[1].id:
            raise NodeManagerError("cannot start")
        return object()

    async def fake_stop_node(_db: AsyncSession, device: Device, caller: str) -> object:
        if device.id == devices[1].id:
            raise RuntimeError("cannot stop")
        return object()

    async def fake_restart_node(_db: AsyncSession, device: Device, caller: str) -> object:
        if device.id == devices[1].id:
            raise NodeManagerError("cannot restart")
        return object()

    monkeypatch.setattr("app.devices.services.bulk._bulk_start_one", fake_start_node)
    monkeypatch.setattr("app.devices.services.bulk._bulk_stop_one", fake_stop_node)
    monkeypatch.setattr("app.devices.services.bulk._bulk_restart_one", fake_restart_node)
    monkeypatch.setattr("app.devices.services.bulk.event_bus.publish", AsyncMock())

    started = await bulk_service.bulk_start_nodes(db_session, [device.id for device in devices])
    stopped = await bulk_service.bulk_stop_nodes(db_session, [device.id for device in devices])
    restarted = await bulk_service.bulk_restart_nodes(db_session, [device.id for device in devices])

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
        "app.devices.services.bulk._load_devices",
        AsyncMock(return_value=[eligible_ok, eligible_fail, ineligible]),
    )
    monkeypatch.setattr("app.devices.services.bulk.event_bus.publish", AsyncMock())
    monkeypatch.setattr(
        "app.devices.services.bulk.resolve_pack_platform",
        AsyncMock(return_value=_resolved),
    )
    monkeypatch.setattr(
        "app.devices.services.bulk.pack_device_lifecycle_action",
        AsyncMock(side_effect=[{"success": True}, AgentCallError("10.0.0.10", "boom")]),
    )

    result = await bulk_service.bulk_reconnect(db, [eligible_ok.id, eligible_fail.id, ineligible.id])

    assert result["succeeded"] == 1
    assert result["failed"] == 2
    assert result["errors"][str(ineligible.id)] == "Not a network-connected Android device"


async def test_bulk_delete_and_maintenance_operations_collect_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    devices = [_device(), _device()]
    db = AsyncMock()
    monkeypatch.setattr("app.devices.services.bulk._load_devices", AsyncMock(return_value=devices))
    monkeypatch.setattr("app.devices.services.bulk.event_bus.publish", AsyncMock())
    monkeypatch.setattr("app.devices.services.bulk.queue_event_for_session", Mock())
    # bulk_enter_maintenance calls device_locking.lock_device(db, ...) which does
    # `(await db.execute(stmt)).scalar_one()`. With db = AsyncMock(), the value
    # returned by `await db.execute(...)` is itself an AsyncMock, so `.scalar_one()`
    # is an AsyncMock attribute — calling it produces a coroutine that nothing
    # awaits, leaking a "coroutine was never awaited" warning that surfaces in a
    # later test during gc. Patch lock_device directly to keep the mock chain
    # purely synchronous past the awaited call.
    monkeypatch.setattr(
        "app.devices.services.bulk.device_locking.lock_device",
        AsyncMock(side_effect=lambda _db, device_id, **_: next(d for d in devices if d.id == device_id)),
    )
    monkeypatch.setattr(
        "app.devices.services.bulk.delete_device",
        AsyncMock(side_effect=[True, False, RuntimeError("cannot delete")]),
    )
    monkeypatch.setattr(
        "app.devices.services.bulk.enter_maintenance",
        AsyncMock(side_effect=[None, RuntimeError("boom")]),
    )
    monkeypatch.setattr(
        "app.devices.services.bulk.exit_maintenance",
        AsyncMock(side_effect=[ValueError("bad state"), RuntimeError("boom")]),
    )

    deleted = await bulk_service.bulk_delete(db, [devices[0].id, devices[1].id, uuid4()])
    entered = await bulk_service.bulk_enter_maintenance(db, [device.id for device in devices])
    exited = await bulk_service.bulk_exit_maintenance(db, [device.id for device in devices])

    assert deleted["failed"] == 2
    assert entered["failed"] == 1
    assert exited["failed"] == 2


async def test_bulk_exit_maintenance_enqueues_recovery_jobs(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """bulk_exit_maintenance must enqueue exactly one recovery job per successfully-exited device.

    This regression test covers the fix-2 window: state mutations are committed
    first (bulk commit) and recovery jobs are only enqueued afterwards, so
    create_job cannot commit mid-loop and strand a device.
    """
    # Create 3 devices in maintenance.
    devices = [
        await create_device(
            db_session,
            host_id=db_host.id,
            name=f"bulk-exit-recovery-{i}",
            hold=DeviceHold.maintenance,
            operational_state=DeviceOperationalState.offline,
        )
        for i in range(3)
    ]
    await db_session.commit()

    result = await bulk_service.bulk_exit_maintenance(db_session, [d.id for d in devices])

    assert result["succeeded"] == 3
    assert result["failed"] == 0

    # Each successfully-exited device must have exactly one recovery job enqueued.
    rows = (await db_session.execute(select(Job).where(Job.kind == JOB_KIND_DEVICE_RECOVERY))).scalars().all()
    assert len(rows) == 3, f"Expected 3 recovery jobs, got {len(rows)}"

    enqueued_device_ids = {row.payload["device_id"] for row in rows}
    expected_device_ids = {str(d.id) for d in devices}
    assert enqueued_device_ids == expected_device_ids, (
        f"Recovery jobs enqueued for wrong device IDs: {enqueued_device_ids!r}"
    )
