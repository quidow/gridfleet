"""Per-phase duration metrics for slow observation loops.

A loop-level duration histogram says a cycle was slow but not why. The phase
histogram attributes cycle wall time (db_prepass / probe / apply / cooldowns)
so a slow cycle can be pinned to a phase instead of guessed at.
"""

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from prometheus_client import REGISTRY

from app.core.metrics_recorders import record_background_loop_phase
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
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


def _phase_count(loop_name: str, phase: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "background_loop_phase_duration_seconds_count",
            {"loop_name": loop_name, "phase": phase},
        )
        or 0.0
    )


def test_recorder_observes_labeled_phase() -> None:
    before = _phase_count("test_loop", "probe")
    record_background_loop_phase("test_loop", "probe", 1.25)
    assert _phase_count("test_loop", "probe") == before + 1


async def test_connectivity_cycle_emits_phase_metrics(db_session: AsyncSession) -> None:
    host = Host(hostname="phase-host", ip="10.0.0.21", os_type="linux", agent_port=5100, status=HostStatus.online)
    db_session.add(host)
    await db_session.flush()
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="phase-dev-001",
        connection_target="phase-dev-001",
        name="Phase Phone",
        os_version="14",
        host_id=host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.commit()

    before = {phase: _phase_count("device_connectivity", phase) for phase in ("db_prepass", "probe", "apply")}

    async def healthy_probe(device: Device, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0)
        return {"healthy": True}

    with (
        patch(
            "app.devices.services.connectivity._get_agent_devices",
            new_callable=AsyncMock,
            return_value={"phase-dev-001"},
        ),
        patch("app.devices.services.connectivity._get_device_health", healthy_probe),
    ):
        await ConnectivityService(
            publisher=Mock(),
            settings=FakeSettingsReader({}),
            circuit_breaker=Mock(),
            lifecycle_policy=AsyncMock(),
            health=DeviceHealthService(publisher=Mock()),
        ).check_connectivity(db_session)

    for phase in ("db_prepass", "probe", "apply"):
        assert _phase_count("device_connectivity", phase) == before[phase] + 1, phase
