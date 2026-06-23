"""Audit M3 step 1: the two independent per-device probe reads
(``_fetch_lifecycle_state`` and ``_get_device_health``) must run concurrently
within a single semaphore slot, so a lifecycle-capable device costs
max(lifecycle, health) instead of the sum. A single device probed serially
peaks at concurrency 1; the in-slot gather peaks at 2.
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
def _noop_assert_current_leader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.devices.services.connectivity.assert_current_leader", AsyncMock(return_value=None))


async def _seed_one_device(db_session: AsyncSession) -> str:
    host = Host(hostname="inslot-host", ip="10.0.0.30", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    target = "inslot-dev"
    with state_write_guard.bypass():
        device = Device(
            pack_id="appium-uiautomator2",
            platform_id="android_mobile",
            identity_scheme="android_serial",
            identity_scope="host",
            identity_value=target,
            connection_target=target,
            name="In-slot Phone",
            os_version="14",
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            verified_at=datetime.now(UTC),
            device_type=DeviceType.real_device,
            connection_type=ConnectionType.usb,
        )
    db_session.add(device)
    await db_session.commit()
    return target


async def test_lifecycle_and_health_probes_overlap_within_slot(db_session: AsyncSession) -> None:
    target = await _seed_one_device(db_session)

    active = 0
    peak = 0

    async def _tracked() -> None:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)  # hold both reads open so the peer can enter the slot
        active -= 1

    async def probing_health(device: Device, **kwargs: object) -> dict[str, object]:
        await _tracked()
        return {"healthy": True}

    async def probing_lifecycle(device: Device, **kwargs: object) -> str:
        await _tracked()
        return "on"

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={target},
        ),
        # Force the single device to be lifecycle-capable so both reads fire.
        patch(
            "app.devices.services.connectivity._lifecycle_state_capable",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("app.devices.services.connectivity._get_device_health", probing_health),
        patch("app.devices.services.connectivity._fetch_lifecycle_state", probing_lifecycle),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ).check_connectivity(db_session)

    assert peak == 2, f"in-slot probe reads serialized (peak={peak}); expected the lifecycle+health gather to overlap"
