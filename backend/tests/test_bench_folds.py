"""In-process load benchmark for the status-push folds.

Reproduces the per-push CPU cost of the two dominant folds
(``fold_host_device_health`` and ``fold_host_nodes``) against a synthetic
fleet, so fold optimizations can be measured deterministically with cProfile
instead of prod py-spy sampling.

Skipped in the normal suite. Run explicitly:

    FOLD_BENCH=1 FOLD_BENCH_DEVICES=50 FOLD_BENCH_ITERS=3 \
        uv run pytest -s -p no:randomly tests/test_bench_folds.py -o addopts=""

Only the agent *network* dial is stubbed; ``_lifecycle_state_capable`` /
``resolve_pack_platform`` run for real (that per-device pack-manifest resolve is
part of what we are measuring).
"""

from __future__ import annotations

import contextlib
import functools
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import event

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host
from app.core.metrics_recorders import HOST_PUSH_OBSERVATION_FAILURES
from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.property_refresh import PropertyRefreshService
from app.hosts.models import Host, HostStatus
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.service_status_push import HostStatusPushService, ObservationFold
from app.packs.services.discovery import PackDiscoveryService
from tests.fakes import FakeSettingsReader
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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
    k = _churn_count(len(devices), churn)
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {
            d.identity: {"healthy": i >= k}  # first k unhealthy
            for i, d in enumerate(devices)
        },
    }


def _node_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    k = _churn_count(len(devices), churn)
    return {
        "reported_at": now_utc().isoformat(),
        "nodes": [
            {
                "port": d.port,
                "pid": d.pid,  # kept matching so the fold does not stale-skip
                "connection_target": d.identity,
                "running": i >= k,  # first k: not running -> "refused" -> health-failure write path
                "observed_at": now_utc().isoformat(),
            }
            for i, d in enumerate(devices)
        ],
    }


def _telemetry_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    k = _churn_count(len(devices), churn)
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {
            d.identity: {
                "observed_at": now_utc().isoformat(),
                "support_status": "supported",
                "battery_level_percent": 5 if i < k else 80,
                "battery_temperature_c": 100.0 if i < k else 30.0,  # first k: critical temp
                "charging_state": "charging",
            }
            for i, d in enumerate(devices)
        },
    }


def _properties_section(devices: list[_SeededDevice], churn: float = 0.0) -> dict[str, object]:
    k = _churn_count(len(devices), churn)
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {
            d.identity: {
                "identity_value": d.identity,
                "detected_properties": {"os_version": d.spec.drift_os_version if i < k else d.spec.os_version},
            }
            for i, d in enumerate(devices)
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
        self.armed = True

    def __call__(self, conn: object, cursor: object, statement: str, *a: object) -> None:
        if not self.armed:
            return
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


def _dial_stubs() -> contextlib.ExitStack:
    """Stub every agent-network dial the unhealthy connectivity path can reach so
    the decision logic runs but no packet leaves. Only fold_host_device_health dials.
    _get_agent_devices MUST return a set (not None) or the device short-circuits and
    the write path is never measured.
    """
    stack = contextlib.ExitStack()
    stack.enter_context(
        patch("app.devices.services.connectivity._fetch_lifecycle_state", new_callable=AsyncMock, return_value=None)
    )
    stack.enter_context(
        # Empty set -> churned devices take the disconnect write path; returning device
        # aliases would instead exercise the escalate path.
        patch("app.devices.services.connectivity._get_agent_devices", new_callable=AsyncMock, return_value=set())
    )
    # Unreachable with the current churn payload (no recommended_action -> no repair
    # dispatch); kept as defensive coverage if a future churn payload adds one.
    stack.enter_context(
        patch("app.devices.services.connectivity._get_device_health", new_callable=AsyncMock, return_value=None)
    )
    stack.enter_context(
        patch(
            "app.devices.services.link_repair.dispatch_recommended_action",
            new_callable=AsyncMock,
            return_value={"success": False},
        )
    )
    return stack


async def _measure(
    label: str,
    *,
    seed: Callable[[int], Awaitable[tuple[Host, list[_SeededDevice]]]],
    run: Callable[[Host, list[_SeededDevice]], Awaitable[None]],
    tap: _QueryTap,
) -> None:
    """Run ITERS timed iterations. Under churn, re-seed a fresh generation per
    iteration so each iteration measures a real transition (re-observing the same
    changed device is a cheap no-op once its escalation state is already set). The
    tap is armed only around the timed run so seed queries are never counted.
    """
    tap.armed = False
    host, devices = await seed(0)
    wall_ms: list[float] = []
    for iteration in range(ITERS):
        if CHURN > 0 and iteration > 0:
            host, devices = await seed(iteration)  # new generation = clean fresh fleet
        tap.armed = True
        t0 = perf_counter()
        await run(host, devices)
        wall_ms.append((perf_counter() - t0) * 1000)
        tap.armed = False
    _report(label, tap, wall_ms)


async def test_bench_device_health_fold(db_session: AsyncSession) -> None:
    service = ConnectivityService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=AsyncMock(),
        health=DeviceHealthService(publisher=Mock()),
    )
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)

    async def _seed(gen: int) -> tuple[Host, list[_SeededDevice]]:
        return await _seed_fleet(db_session, FLEET, DEVICES, generation=gen)

    async def _run(host: Host, devices: list[_SeededDevice]) -> None:
        await service.fold_host_device_health(db_session, host.id, _device_section(devices, CHURN))

    with _dial_stubs():
        await _measure("fold_host_device_health", seed=_seed, run=_run, tap=tap)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


async def test_bench_node_health_fold(db_session: AsyncSession) -> None:
    service = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)

    async def _seed(gen: int) -> tuple[Host, list[_SeededDevice]]:
        return await _seed_fleet(db_session, FLEET, DEVICES, generation=gen)

    async def _run(host: Host, devices: list[_SeededDevice]) -> None:
        await service.fold_host_nodes(db_session, host.id, _node_section(devices, CHURN))

    await _measure("fold_host_nodes", seed=_seed, run=_run, tap=tap)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


async def test_bench_device_telemetry_fold(db_session: AsyncSession) -> None:
    service = HardwareTelemetryService(publisher=Mock(), settings=FakeSettingsReader({}))
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)

    async def _seed(gen: int) -> tuple[Host, list[_SeededDevice]]:
        return await _seed_fleet(db_session, FLEET, DEVICES, generation=gen)

    async def _run(host: Host, devices: list[_SeededDevice]) -> None:
        await service.fold_host_device_telemetry(db_session, host.id, _telemetry_section(devices, CHURN))

    await _measure("fold_host_device_telemetry", seed=_seed, run=_run, tap=tap)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


async def test_bench_device_properties_fold(db_session: AsyncSession) -> None:
    discovery = PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(),
        circuit_breaker=Mock(),
        serializer=Mock(),
        identity_guard=AsyncMock(),
    )
    service = PropertyRefreshService(discovery=discovery)
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)

    async def _seed(gen: int) -> tuple[Host, list[_SeededDevice]]:
        return await _seed_fleet(db_session, FLEET, DEVICES, generation=gen)

    async def _run(host: Host, devices: list[_SeededDevice]) -> None:
        await service.fold_host_device_properties(db_session, host.id, _properties_section(devices, CHURN))

    await _measure("fold_host_device_properties", seed=_seed, run=_run, tap=tap)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


def _host_telemetry_sample(iteration: int) -> dict[str, object]:
    # Advance recorded_at past the 60 s rate-limit each iteration so every
    # iteration performs a real insert rather than being skipped.
    recorded_at = now_utc() + timedelta(seconds=iteration * 120)
    return {
        "recorded_at": recorded_at.isoformat(),
        "cpu_percent": 42.0,
        "memory_used_mb": 8000,
        "memory_total_mb": 16000,
        "disk_used_gb": 100.0,
        "disk_total_gb": 500.0,
        "disk_percent": 20.0,
    }


async def test_bench_host_telemetry_fold(db_session: AsyncSession) -> None:
    service = HostResourceTelemetryService(settings=FakeSettingsReader({}))
    tap = _QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)
    tap.armed = False  # exclude the one-time seed from the per-push query count
    host, _devices = await _seed_fleet(db_session, FLEET, DEVICES)
    wall_ms: list[float] = []
    for iteration in range(ITERS):
        tap.armed = True
        t0 = perf_counter()
        await service.fold_host_telemetry(db_session, host.id, _host_telemetry_sample(iteration))
        wall_ms.append((perf_counter() - t0) * 1000)
        tap.armed = False
    _report("fold_host_telemetry", tap, wall_ms)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


class _CommitTap:
    def __init__(self) -> None:
        self.count = 0
        self.armed = True

    def __call__(self, conn: object) -> None:
        if self.armed:
            self.count += 1


def _observation_failure_total() -> float:
    """Sum every child counter of HOST_PUSH_OBSERVATION_FAILURES. process_observations
    swallows per-stage exceptions and bumps this; a stubbing gap would silently skip a
    stage and undercount, so the whole-push bench asserts this does not rise.

    Catches STAGE-level failures only: restart ingest, convergence, and each fold that
    raises out of process_observations (including the dial-seam-bearing device_health
    fold). It does NOT catch per-device errors that the telemetry/properties/host_telemetry
    folds swallow internally via db.rollback() + logger.exception -- acceptable here
    because those three folds have no agent-dial seams for a stubbing gap to break."""
    return sum(
        sample.value
        for metric in HOST_PUSH_OBSERVATION_FAILURES.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    )


def _build_push_service(session_factory: async_sessionmaker[AsyncSession]) -> HostStatusPushService:
    settings = FakeSettingsReader({})
    node_health = NodeHealthService(
        publisher=event_bus,
        settings=settings,
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    connectivity = ConnectivityService(
        publisher=event_bus,
        settings=settings,
        circuit_breaker=Mock(),
        lifecycle_policy=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
    )
    hardware_telemetry = HardwareTelemetryService(publisher=event_bus, settings=settings)
    discovery = PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(), circuit_breaker=Mock(), serializer=Mock(), identity_guard=AsyncMock()
    )
    property_refresh = PropertyRefreshService(discovery=discovery)
    resource_telemetry = HostResourceTelemetryService(settings=settings)
    reconciler = ReconcilerService(
        publisher=event_bus, settings=settings, pool=None, circuit_breaker=Mock(), session_factory=session_factory
    )
    heartbeat = HeartbeatService(
        publisher=event_bus, settings=settings, pool=Mock(), circuit_breaker=Mock(), session_factory=session_factory
    )
    return HostStatusPushService(
        publisher=event_bus,
        session_factory=session_factory,
        observation_folds=(
            ObservationFold("node_health", node_health.fold_host_nodes),
            ObservationFold("device_health", connectivity.fold_host_device_health),
            ObservationFold("device_telemetry", hardware_telemetry.fold_host_device_telemetry),
            ObservationFold("device_properties", property_refresh.fold_host_device_properties),
            ObservationFold("host_telemetry", resource_telemetry.fold_host_telemetry),
        ),
        converge_host=functools.partial(converge_pushed_host, session_factory=session_factory, reconciler=reconciler),
        ingest_restart_events=heartbeat.ingest_restart_events,
    )


def _consolidated_payload(devices: list[_SeededDevice], churn: float, iteration: int) -> dict[str, object]:
    node_section = _node_section(devices, churn)
    return {
        "appium_processes": {
            "nodes": node_section["nodes"],
            "recent_restart_events": [],
            "start_failures": [],
        },
        "host_telemetry": _host_telemetry_sample(iteration),
        "node_health": node_section,
        "device_health": _device_section(devices, churn),
        "device_telemetry": _telemetry_section(devices, churn),
        "device_properties": _properties_section(devices, churn),
    }


def _report_whole_push(tap: _QueryTap, commits: _CommitTap, wall_ms: list[float]) -> None:
    avg = sum(wall_ms) / len(wall_ms)
    q_per_push = tap.total / ITERS
    print(f"\n{'=' * 78}\nwhole_push (all stages): {DEVICES} devices x {ITERS} iters  churn={CHURN}")
    print(f"  wall per push:     avg {avg:.1f} ms   ({', '.join(f'{w:.0f}' for w in wall_ms)})")
    print(f"  QUERIES per push:  {q_per_push:.0f}   ({q_per_push / DEVICES:.2f} per device)")
    print(f"  COMMITS per push:  {commits.count / ITERS:.1f}")
    print("  top statements per push:")
    for sig, n in tap.counter.most_common(18):
        print(f"    {n / ITERS:8.1f}  {sig}")


async def test_bench_whole_push(db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]) -> None:
    service = _build_push_service(db_session_maker)
    tap = _QueryTap()
    commits = _CommitTap()
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "commit", commits)
    failures_before = _observation_failure_total()

    tap.armed = False
    commits.armed = False
    host, devices = await _seed_fleet(db_session, FLEET, DEVICES, generation=0)
    await db_session.commit()  # ensure the seed is visible to factory-opened sessions
    wall_ms: list[float] = []
    with _dial_stubs():
        for iteration in range(ITERS):
            if CHURN > 0 and iteration > 0:
                host, devices = await _seed_fleet(db_session, FLEET, DEVICES, generation=iteration)
                await db_session.commit()
            payload = _consolidated_payload(devices, CHURN, iteration)
            tap.armed = True
            commits.armed = True
            t0 = perf_counter()
            await service.process_observations(
                host_id=host.id, host_ip=host.ip, agent_port=host.agent_port, payload=payload
            )
            wall_ms.append((perf_counter() - t0) * 1000)
            tap.armed = False
            commits.armed = False

    event.remove(engine, "before_cursor_execute", tap)
    event.remove(engine, "commit", commits)
    _report_whole_push(tap, commits, wall_ms)
    # Guard: a stubbing gap would make process_observations silently skip a stage.
    assert _observation_failure_total() == failures_before, "a whole-push stage failed (check dial stubs / wiring)"
