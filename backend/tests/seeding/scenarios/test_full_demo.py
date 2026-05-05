from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models.appium_node import AppiumNode, NodeState
from app.models.config_audit_log import ConfigAuditLog
from app.models.device import ConnectionType, Device, DeviceHold, DeviceOperationalState
from app.models.device_reservation import DeviceReservation
from app.models.driver_pack import DriverPack, DriverPackPlatform, DriverPackRelease
from app.models.host import Host, HostStatus
from app.models.host_pack_installation import HostPackDoctorResult, HostPackInstallation
from app.models.host_runtime_installation import HostRuntimeInstallation
from app.models.host_terminal_session import HostTerminalSession
from app.models.session import Session, SessionStatus
from app.models.setting import Setting
from app.models.test_run import RunState, TestRun
from app.seeding.context import SeedContext
from app.seeding.scenarios.full_demo import apply_full_demo
from app.services import device_presenter
from app.services.settings_registry import SETTINGS_REGISTRY


@pytest.mark.asyncio
async def test_full_demo_scenario_invariants(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_full_demo(ctx, skip_telemetry=True)
    await db_session.commit()

    hosts = (await db_session.execute(select(Host))).scalars().all()
    assert len(hosts) == 4
    assert sum(1 for h in hosts if h.status is HostStatus.offline) == 1

    devices = (await db_session.execute(select(Device))).scalars().all()
    assert len(devices) == 35

    # Count by platform_id prefix instead of legacy Platform enum
    platform_id_counts: dict[str, int] = {}
    for d in devices:
        platform_id_counts[d.platform_id] = platform_id_counts.get(d.platform_id, 0) + 1

    android_mobile_count = sum(v for k, v in platform_id_counts.items() if k.startswith("android_mobile"))
    android_tv_count = sum(v for k, v in platform_id_counts.items() if k.startswith("android_tv"))
    ios_count = sum(v for k, v in platform_id_counts.items() if k.startswith("ios"))
    tvos_count = sum(v for k, v in platform_id_counts.items() if k.startswith("tvos"))
    firetv_count = sum(v for k, v in platform_id_counts.items() if k.startswith("firetv"))
    assert android_mobile_count == 15
    assert android_tv_count == 3
    assert ios_count == 9
    assert tvos_count == 3
    assert firetv_count == 3
    assert any(k == "roku_network" for k in platform_id_counts)

    total_runs = await db_session.scalar(select(func.count()).select_from(TestRun))
    assert total_runs is not None
    assert 450 <= total_runs <= 550
    active_runs = await db_session.scalar(
        select(func.count()).select_from(TestRun).where(TestRun.state == RunState.active)
    )
    assert active_runs == 2 or active_runs == 3  # 2% of ~500 with jitter

    open_reservations = await db_session.scalar(
        select(func.count()).select_from(DeviceReservation).where(DeviceReservation.released_at.is_(None))
    )
    assert open_reservations is not None and open_reservations >= active_runs


@pytest.mark.asyncio
async def test_full_demo_seeds_realistic_live_states_and_network_metadata(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_full_demo(ctx, skip_telemetry=True)
    await db_session.commit()

    devices = (await db_session.execute(select(Device).order_by(Device.created_at, Device.id))).scalars().all()
    payloads = [await device_presenter.serialize_device(db_session, device) for device in devices]

    chip_statuses = {payload["hold"] or payload["operational_state"] for payload in payloads}
    assert len(chip_statuses) >= 3
    assert {payload["readiness_state"] for payload in payloads} >= {
        "setup_required",
        "verification_required",
        "verified",
    }

    active_session_devices = (
        (
            await db_session.execute(
                select(Device)
                .join(Session, Session.device_id == Device.id)
                .join(TestRun, TestRun.id == Session.run_id)
                .where(
                    TestRun.state == RunState.active,
                    Session.status == SessionStatus.running,
                    Session.ended_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert active_session_devices
    assert all(device.operational_state is DeviceOperationalState.busy for device in active_session_devices)

    network_devices = [device for device in devices if device.connection_type is ConnectionType.network]
    assert network_devices
    assert all(device.ip_address for device in network_devices)
    assert all("ip_address" not in (device.tags or {}) for device in network_devices)

    assert any(payload["platform_id"] == "roku_network" for payload in payloads)


@pytest.mark.asyncio
async def test_full_demo_uses_current_settings_registry_keys(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_full_demo(ctx, skip_telemetry=True)
    await db_session.commit()

    settings = (await db_session.execute(select(Setting).order_by(Setting.key))).scalars().all()

    assert settings
    assert {setting.key for setting in settings} <= set(SETTINGS_REGISTRY)


@pytest.mark.asyncio
async def test_full_demo_seeds_pack_runtime_and_node_surfaces(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_full_demo(ctx, skip_telemetry=True)
    await db_session.commit()

    packs = (await db_session.execute(select(DriverPack).order_by(DriverPack.id))).scalars().all()
    assert {pack.id for pack in packs} == {"appium-uiautomator2", "appium-roku-dlenroc", "appium-xcuitest"}
    expected_releases = {
        "appium-uiautomator2": "2026.04.0",
        "appium-roku-dlenroc": "2026.04.0",
        "appium-xcuitest": "2026.04.12",
    }
    assert {pack.id: pack.current_release for pack in packs} == expected_releases

    platform_rows = (
        await db_session.execute(
            select(DriverPackPlatform, DriverPackRelease.pack_id).join(
                DriverPackRelease,
                DriverPackRelease.id == DriverPackPlatform.pack_release_id,
            )
        )
    ).all()
    pack_platforms = {(pack_id, platform.manifest_platform_id) for platform, pack_id in platform_rows}
    devices = (await db_session.execute(select(Device))).scalars().all()
    assert {(device.pack_id, device.platform_id) for device in devices} <= pack_platforms

    runtimes = (await db_session.execute(select(HostRuntimeInstallation))).scalars().all()
    installs = (await db_session.execute(select(HostPackInstallation))).scalars().all()
    doctor_results = (await db_session.execute(select(HostPackDoctorResult))).scalars().all()
    assert runtimes
    assert installs
    assert doctor_results
    assert {install.status for install in installs} >= {"installed", "blocked"}

    nodes = (await db_session.execute(select(AppiumNode).order_by(AppiumNode.port))).scalars().all()
    assert nodes
    running_nodes = [node for node in nodes if node.state is NodeState.running]
    assert len({node.port for node in running_nodes}) == len(running_nodes)
    device_by_id = {device.id: device for device in devices}
    host_by_id = {host.id: host for host in (await db_session.execute(select(Host))).scalars().all()}
    for node in running_nodes:
        device = device_by_id[node.device_id]
        assert device.verified_at is not None
        assert device.operational_state is not DeviceOperationalState.offline
        assert device.hold is not DeviceHold.maintenance
        assert host_by_id[device.host_id].status is HostStatus.online
        assert node.active_connection_target == device.connection_target


@pytest.mark.asyncio
async def test_full_demo_seeds_operator_history_surfaces(db_session) -> None:  # noqa: ANN001
    ctx = SeedContext.build(session=db_session, seed=42)
    await apply_full_demo(ctx, skip_telemetry=True)
    await db_session.commit()

    assert await db_session.scalar(select(func.count()).select_from(ConfigAuditLog)) == 3
    assert await db_session.scalar(select(func.count()).select_from(HostTerminalSession)) == 3
