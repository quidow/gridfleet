"""In-process load benchmark for the status-push folds.

Reproduces the per-push CPU cost of the two dominant folds
(``fold_host_device_health`` and ``fold_host_nodes``) against a synthetic
fleet, so fold optimizations can be measured deterministically with cProfile
instead of prod py-spy sampling.

Skipped in the normal suite. Run explicitly:

    FOLD_BENCH=1 FOLD_BENCH_DEVICES=100 FOLD_BENCH_ITERS=3 \
        uv run pytest -s -p no:randomly tests/test_bench_folds.py -o addopts=""

Only the agent *network* dial is stubbed; ``_lifecycle_state_capable`` /
``resolve_pack_platform`` run for real (that per-device pack-manifest resolve is
part of what we are measuring).
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from time import perf_counter
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import event

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.node_health import NodeHealthService
from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.property_refresh import PropertyRefreshService
from app.hosts.models import Host, HostStatus
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.packs.services.discovery import PackDiscoveryService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("seeded_driver_packs"),
    pytest.mark.skipif(not os.getenv("FOLD_BENCH"), reason="set FOLD_BENCH=1 to run the fold load benchmark"),
]

DEVICES = int(os.getenv("FOLD_BENCH_DEVICES", "50"))
ITERS = int(os.getenv("FOLD_BENCH_ITERS", "3"))
CHURN = float(os.getenv("FOLD_BENCH_CHURN", "0.0"))


@dataclass(frozen=True)
class _TupleSpec:
    """One (pack, platform, device_type, connection) shape a host runs."""

    pack_id: str
    platform_id: str
    device_type: DeviceType
    connection_type: ConnectionType
    identity_scheme: str
    os_version: str
    drift_os_version: str  # property-churn target; must differ from os_version


@dataclass(frozen=True)
class _SeededDevice:
    identity: str  # identity_value == connection_target
    port: int
    pid: int
    spec: _TupleSpec


# Mixed-per-host default: two distinct (pack, platform, device_type) tuples so
# the connectivity fold's pack_platform_resolution_cache and preloaded catalog
# take the cache-MISS path they take in a real mixed deployment. Both USB, so no
# network-device identity-rewrite path is involved.
_MIXED_FLEET: tuple[_TupleSpec, ...] = (
    _TupleSpec(
        "appium-uiautomator2",
        "android_mobile",
        DeviceType.real_device,
        ConnectionType.usb,
        "android_serial",
        "14",
        "15",
    ),
    _TupleSpec("appium-xcuitest", "ios", DeviceType.real_device, ConnectionType.usb, "apple_udid", "17", "18"),
)
# Baseline: today's uniform shape, for mixed-vs-homogeneous cache comparison.
_HOMOGENEOUS_FLEET: tuple[_TupleSpec, ...] = (_MIXED_FLEET[0],)

FLEET: tuple[_TupleSpec, ...] = (
    _HOMOGENEOUS_FLEET if os.getenv("FOLD_BENCH_FLEET", "mixed") == "homogeneous" else _MIXED_FLEET
)


def _churn_count(n: int, churn: float) -> int:
    """First-k selection size for a churn fraction (deterministic, index-based)."""
    return round(n * churn)


async def _seed_fleet(
    db: AsyncSession, specs: tuple[_TupleSpec, ...], n: int, generation: int = 0
) -> tuple[Host, list[_SeededDevice]]:
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
    seeded: list[_SeededDevice] = []
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
        seeded.append(_SeededDevice(identity=ident, port=4723 + i, pid=1000 + i, spec=spec))
    await db.commit()
    return host, seeded


def _device_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {d.identity: {"healthy": True} for d in devices},
    }


def _node_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    return {
        "reported_at": now_utc().isoformat(),
        "nodes": [
            {
                "port": d.port,
                "pid": d.pid,
                "connection_target": d.identity,
                "running": True,
                "observed_at": now_utc().isoformat(),
            }
            for d in devices
        ],
    }


def _telemetry_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {
            d.identity: {
                "observed_at": now_utc().isoformat(),
                "support_status": "supported",
                "battery_level_percent": 80,
                "battery_temperature_c": 30.0,
                "charging_state": "charging",
            }
            for d in devices
        },
    }


def _properties_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    # Steady state: detected os_version matches the seeded value, so
    # apply_pack_device_properties finds nothing changed and never commits.
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {
            d.identity: {
                "identity_value": d.identity,
                "detected_properties": {"os_version": d.spec.os_version},
            }
            for d in devices
        },
    }


_WS = re.compile(r"\s+")


def _signature(sql: str) -> str:
    """Collapse a statement to verb + first table so round-trips group by kind."""
    s = _WS.sub(" ", sql.strip())
    m = re.match(r"(?i)(SELECT|INSERT INTO|UPDATE|DELETE FROM)\s+([^\s(]+)?", s)
    if not m:
        return s[:48]
    verb = m.group(1).upper().split()[0]
    if verb == "SELECT":
        tbl = re.search(r"(?i)\bFROM\s+([^\s(]+)", s)
        return f"SELECT {tbl.group(1) if tbl else '?'}"
    return f"{verb} {m.group(2) or '?'}"


class _QueryTap:
    def __init__(self) -> None:
        self.counter: Counter[str] = Counter()
        self.total = 0

    def __call__(self, conn: object, cursor: object, statement: str, *a: object) -> None:
        self.total += 1
        self.counter[_signature(statement)] += 1


def _report(label: str, tap: _QueryTap, wall_ms: list[float]) -> None:
    avg = sum(wall_ms) / len(wall_ms)
    q_per_push = tap.total / ITERS
    print(f"\n{'=' * 78}\n{label}: {DEVICES} devices x {ITERS} iters")
    print(f"  wall per push:    avg {avg:.1f} ms   ({', '.join(f'{w:.0f}' for w in wall_ms)})")
    print(f"  QUERIES per push: {q_per_push:.0f}   ({q_per_push / DEVICES:.2f} per device)")
    print("  top statements per push:")
    for sig, n in tap.counter.most_common(18):
        print(f"    {n / ITERS:8.1f}  {sig}")


async def test_bench_device_health_fold(db_session: AsyncSession) -> None:
    host, devices = await _seed_fleet(db_session, FLEET, DEVICES)
    service = ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=AsyncMock(),
        health=DeviceHealthService(publisher=Mock()),
    )
    section = _device_section(devices, CHURN)
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)
    wall_ms: list[float] = []
    with patch("app.devices.services.connectivity._fetch_lifecycle_state", new_callable=AsyncMock, return_value=None):
        for _ in range(ITERS):
            t0 = perf_counter()
            await service.fold_host_device_health(db_session, host.id, section)
            wall_ms.append((perf_counter() - t0) * 1000)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)
    _report("fold_host_device_health", tap, wall_ms)


async def test_bench_node_health_fold(db_session: AsyncSession) -> None:
    host, devices = await _seed_fleet(db_session, FLEET, DEVICES)
    service = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    section = _node_section(devices, CHURN)
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)
    wall_ms: list[float] = []
    for _ in range(ITERS):
        t0 = perf_counter()
        await service.fold_host_nodes(db_session, host.id, section)
        wall_ms.append((perf_counter() - t0) * 1000)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)
    _report("fold_host_nodes", tap, wall_ms)


async def test_bench_device_telemetry_fold(db_session: AsyncSession) -> None:
    host, devices = await _seed_fleet(db_session, FLEET, DEVICES)
    service = HardwareTelemetryService(publisher=Mock(), settings=FakeSettingsReader({}))
    section = _telemetry_section(devices, CHURN)
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)
    wall_ms: list[float] = []
    for _ in range(ITERS):
        t0 = perf_counter()
        await service.fold_host_device_telemetry(db_session, host.id, section)
        wall_ms.append((perf_counter() - t0) * 1000)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)
    _report("fold_host_device_telemetry", tap, wall_ms)


async def test_bench_device_properties_fold(db_session: AsyncSession) -> None:
    host, devices = await _seed_fleet(db_session, FLEET, DEVICES)
    discovery = PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(),
        circuit_breaker=Mock(),
        serializer=Mock(),
        identity_guard=Mock(),
    )
    service = PropertyRefreshService(discovery=discovery)
    section = _properties_section(devices, CHURN)
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)
    wall_ms: list[float] = []
    for _ in range(ITERS):
        t0 = perf_counter()
        await service.fold_host_device_properties(db_session, host.id, section)
        wall_ms.append((perf_counter() - t0) * 1000)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)
    _report("fold_host_device_properties", tap, wall_ms)
