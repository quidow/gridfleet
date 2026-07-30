"""Fleet seeding and fold-section fixtures shared by the fold benchmark and the ordinary suite.

Extracted from ``tests/test_bench_folds.py`` so the one-commit outbox budget
assertion can run in CI: that module is skipped unless ``FOLD_BENCH`` is set,
and nothing under ``.github/`` sets it. Names are public here because they cross
a module boundary now; the benchmark's own env-driven knobs (``FLEET``,
``LIFECYCLE_MODE``, the scenario table) stay in the benchmark.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.hosts.models import Host, HostStatus
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs.service_reservation import RunReservationService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


def build_real_lifecycle_connectivity_service() -> ConnectivityService:
    incidents = LifecycleIncidentService(publisher=event_bus)
    reservation = RunReservationService()
    actions = LifecyclePolicyActionsService(
        publisher=event_bus,
        reservation=reservation,
        incidents=incidents,
    )
    lifecycle_policy = LifecyclePolicyService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=actions,
        incidents=incidents,
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )
    return ConnectivityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=lifecycle_policy,
        health=DeviceHealthService(publisher=event_bus),
    )


@dataclass(frozen=True)
class TupleSpec:
    """One (pack, platform, device_type, connection) shape a host runs."""

    pack_id: str
    platform_id: str
    device_type: DeviceType
    connection_type: ConnectionType
    identity_scheme: str
    os_version: str
    drift_os_version: str  # property-churn target; must differ from os_version


@dataclass(frozen=True)
class SeededDevice:
    device_id: uuid.UUID
    identity: str  # identity_value == connection_target
    port: int
    pid: int
    spec: TupleSpec


# Mixed-per-host default: two distinct (pack, platform, device_type) tuples so
# the connectivity fold's pack_platform_resolution_cache and preloaded catalog
# take the cache-MISS path they take in a real mixed deployment. Both USB, so no
# network-device identity-rewrite path is involved.
MIXED_FLEET: tuple[TupleSpec, ...] = (
    TupleSpec(
        "appium-uiautomator2",
        "android_mobile",
        DeviceType.real_device,
        ConnectionType.usb,
        "android_serial",
        "14",
        "15",
    ),
    TupleSpec("appium-xcuitest", "ios", DeviceType.real_device, ConnectionType.usb, "apple_udid", "17", "18"),
)
# Baseline: today's uniform shape, for mixed-vs-homogeneous cache comparison.
HOMOGENEOUS_FLEET: tuple[TupleSpec, ...] = (MIXED_FLEET[0],)


async def seed_fleet(
    db: AsyncSession, specs: tuple[TupleSpec, ...], n: int, generation: int = 0
) -> tuple[Host, list[SeededDevice]]:
    # generation makes hostname + identity_value unique so churn re-seeds are a
    # clean fleet (hostname is globally unique; identity_value keys the fold's
    # control-plane escalation state, so it must not repeat across generations).
    host = Host(
        hostname=f"bench-host-g{generation}",
        ip="10.0.0.10",
        os_type="linux",
        agent_port=5100,
        status=HostStatus.online,
    )
    db.add(host)
    await db.flush()
    seeded: list[SeededDevice] = []
    for i in range(n):
        spec = specs[i % len(specs)]  # round-robin, deterministic
        ident = f"bench-g{generation}-{i:04d}"
        device = Device(
            pack_id=spec.pack_id,
            platform_id=spec.platform_id,
            identity_scheme=spec.identity_scheme,
            identity_scope="host",
            identity_value=ident,
            connection_target=ident,
            name=f"Bench Device {i}",
            os_version=spec.os_version,
            host_id=host.id,
            operational_state=DeviceOperationalState.available,
            device_checks_healthy=True,
            verified_at=now_utc(),
            device_type=spec.device_type,
            connection_type=spec.connection_type,
        )
        db.add(device)
        await db.flush()
        db.add(
            AppiumNode(
                device_id=device.id,
                port=4723 + i,
                desired_state=AppiumDesiredState.running,
                desired_port=4723 + i,
                pid=1000 + i,
                active_connection_target=ident,
                health_running=True,
                last_health_checked_at=now_utc(),
                last_observed_at=now_utc(),
            )
        )
        seeded.append(
            SeededDevice(
                device_id=device.id,
                identity=ident,
                port=4723 + i,
                pid=1000 + i,
                spec=spec,
            )
        )
    await db.commit()
    return host, seeded


def device_health_loop_section(
    devices: list[SeededDevice],
    *,
    unhealthy_count: int,
    revision: int,
    section_sequence: int,
) -> dict[str, object]:
    return {
        "reported_at": now_utc().isoformat(),
        "section_sequence": section_sequence,
        OBSERVATION_REVISION_KEY: revision,
        "complete_gather": True,
        "devices": [
            {
                "device_id": str(device.device_id),
                "probe_status": "observed",
                "presence": "present",
                "health": {"healthy": index >= unhealthy_count, "checks": []},
                "lifecycle_state": {"status": "unsupported", "value": None},
            }
            for index, device in enumerate(devices)
        ],
    }
