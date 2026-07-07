"""Connectivity health probes for devices on the same host must run concurrently.

A serial probe loop makes each cycle O(devices) in agent round-trip latency, which
degrades reconciliation timeliness as a host's device count grows. The probe phase
is expected to overlap probes (bounded per host), mirroring the session_sync sweep.
"""

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services import state_write_guard
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.hosts.models import Host, HostStatus
from tests.fakes import FakeSettingsReader

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


@pytest.fixture(autouse=True)
def _skip_lifecycle_state_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.connectivity._fetch_lifecycle_state", AsyncMock(return_value=None))


async def _seed_host_with_devices(db_session: AsyncSession, count: int) -> tuple[Host, list[str]]:
    host = Host(hostname="probe-host", ip="10.0.0.20", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()

    targets: list[str] = []
    for i in range(count):
        target = f"pc-{i:03d}"
        with state_write_guard.bypass():
            device = Device(
                pack_id="appium-uiautomator2",
                platform_id="android_mobile",
                identity_scheme="android_serial",
                identity_scope="host",
                identity_value=target,
                connection_target=target,
                name=f"Phone {i}",
                os_version="14",
                host_id=host.id,
                operational_state=DeviceOperationalState.available,
                verified_at=datetime.now(UTC),
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
            )
        db_session.add(device)
        targets.append(target)
    await db_session.commit()
    return host, targets


async def test_health_probes_run_concurrently_within_host(db_session: AsyncSession) -> None:
    _host, targets = await _seed_host_with_devices(db_session, count=2)

    active = 0
    peak = 0

    async def probing_health(device: Device, **kwargs: object) -> dict[str, object]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)  # hold the probe open so a concurrent peer can enter
        active -= 1
        return {"healthy": True}

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(targets),
        ),
        patch("app.devices.services.connectivity._get_device_health", probing_health),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ).check_connectivity(db_session)

    assert peak >= 2, f"health probes serialized (peak concurrency={peak}); expected overlap >= 2"


async def test_probe_concurrency_respects_settings_knob(db_session: AsyncSession) -> None:
    """general.probe_concurrency_per_host = 1 must serialize the probe phase."""
    _host, targets = await _seed_host_with_devices(db_session, count=3)

    active = 0
    peak = 0

    async def probing_health(device: Device, **kwargs: object) -> dict[str, object]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return {"healthy": True}

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(targets),
        ),
        patch("app.devices.services.connectivity._get_device_health", probing_health),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({"general.probe_concurrency_per_host": 1}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ).check_connectivity(db_session)

    assert peak == 1, f"expected serialized probes with knob=1, got peak={peak}"


async def _seed_two_hosts_one_device_each(db_session: AsyncSession) -> list[str]:
    """Two online hosts, one available device each (distinct host_ids)."""
    targets: list[str] = []
    for h in range(2):
        host = Host(
            hostname=f"xhost-{h}",
            ip=f"10.0.1.{h}",
            os_type="linux",
            agent_port=5100,
            status=HostStatus.online,
        )
        db_session.add(host)
        await db_session.flush()
        target = f"xh{h}-dev"
        with state_write_guard.bypass():
            device = Device(
                pack_id="appium-uiautomator2",
                platform_id="android_mobile",
                identity_scheme="android_serial",
                identity_scope="host",
                identity_value=target,
                connection_target=target,
                name=f"X Phone {h}",
                os_version="14",
                host_id=host.id,
                operational_state=DeviceOperationalState.available,
                verified_at=datetime.now(UTC),
                device_type=DeviceType.real_device,
                connection_type=ConnectionType.usb,
            )
        db_session.add(device)
        targets.append(target)
    await db_session.commit()
    return targets


async def test_health_probes_run_concurrently_across_hosts(db_session: AsyncSession) -> None:
    """Devices on DIFFERENT hosts must probe concurrently even with the per-host
    knob = 1. The old sequential per-host loop serialized hosts (peak == 1); the
    cross-host gather overlaps them (peak == 2)."""
    targets = await _seed_two_hosts_one_device_each(db_session)

    active = 0
    peak = 0

    async def probing_health(device: Device, **kwargs: object) -> dict[str, object]:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)  # hold the probe open so a peer on another host can enter
        active -= 1
        return {"healthy": True}

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value=set(targets),
        ),
        patch("app.devices.services.connectivity._get_device_health", probing_health),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({"general.probe_concurrency_per_host": 1}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ).check_connectivity(db_session)

    assert peak == 2, f"hosts probed serially (peak={peak}); expected cross-host overlap == 2"
