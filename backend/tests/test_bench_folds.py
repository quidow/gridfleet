"""In-process load benchmark for the status-push folds.

Reproduces the per-push CPU cost of the synchronous status-push folds and the
node-health fold against a synthetic fleet, so fold optimizations can be
measured deterministically with cProfile instead of prod py-spy sampling.

Skipped in the normal suite. Run explicitly:

    FOLD_BENCH=1 FOLD_BENCH_DEVICES=50 FOLD_BENCH_ITERS=3 \
        uv run pytest -s -p no:randomly tests/test_bench_folds.py -o addopts=""

The device-health loop benchmark uses the production lifecycle policy by
default. Set ``FOLD_BENCH_LIFECYCLE=isolated`` to retain the core-only profile
with lifecycle hooks mocked.

Set ``FOLD_BENCH_WARMUP`` (default 1) to control how many untimed/unarmed
iterations the device-health loop benchmark runs before the timed ``ITERS``
iterations begin.

The benchmark exercises only facts-backed folds; the asynchronous device-health
fold is measured separately by the StatusFoldLoop benchmark.
"""

from __future__ import annotations

import functools
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host
from app.core.metrics_recorders import HOST_PUSH_OBSERVATION_FAILURES
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.review import ReviewService
from app.hosts.models import Host, HostStatus
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY, HostStatusPushService, ObservationFold
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.packs.services.discovery import PackDiscoveryService
from app.runs.service_reservation import RunReservationService
from tests.bench_instrumentation import (
    CommitTap,
    QueryTap,
    install_async_session_callsite_profiler,
    percentile,
)
from tests.fakes import FakeSettingsReader
from tests.helpers import build_connectivity_service, settle_after_commit_tasks
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
WARMUP = int(os.getenv("FOLD_BENCH_WARMUP", "1"))
CHURN = float(os.getenv("FOLD_BENCH_CHURN", "0.0"))
_raw_lifecycle_mode = os.getenv("FOLD_BENCH_LIFECYCLE", "real")
if _raw_lifecycle_mode not in ("real", "isolated"):
    raise ValueError("FOLD_BENCH_LIFECYCLE must be 'real' or 'isolated'")
LIFECYCLE_MODE = cast("Literal['real', 'isolated']", _raw_lifecycle_mode)


def _build_real_lifecycle_connectivity_service() -> ConnectivityService:
    review = ReviewService()
    incidents = LifecycleIncidentService(publisher=event_bus)
    reservation = RunReservationService(review=review)
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
        review=review,
    )
    return ConnectivityService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        circuit_breaker=Mock(),
        lifecycle_policy=lifecycle_policy,
        health=DeviceHealthService(publisher=event_bus),
    )


def _build_device_health_benchmark_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> ConnectivityService:
    if LIFECYCLE_MODE == "isolated":
        return build_connectivity_service(session_factory)
    return _build_real_lifecycle_connectivity_service()


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
    device_id: uuid.UUID
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
        seeded.append(
            _SeededDevice(
                device_id=device.id,
                identity=ident,
                port=4723 + i,
                pid=1000 + i,
                spec=spec,
            )
        )
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


def _device_health_loop_section(
    devices: list[_SeededDevice],
    churn: float,
    *,
    revision: int,
    section_sequence: int,
) -> dict[str, object]:
    unhealthy_count = _churn_count(len(devices), churn)
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


def _report(label: str, tap: QueryTap, wall_ms: list[float]) -> None:
    avg = sum(wall_ms) / len(wall_ms)
    q_per_push = tap.total / ITERS
    print(f"\n{'=' * 78}\n{label}: {DEVICES} devices x {ITERS} iters")
    print(f"  wall per push:    avg {avg:.1f} ms   ({', '.join(f'{w:.0f}' for w in wall_ms)})")
    print(f"  QUERIES per push: {q_per_push:.0f}   ({q_per_push / DEVICES:.2f} per device)")
    print("  top statements per push:")
    for sig, n in tap.counter.most_common(18):
        print(f"    {n / ITERS:8.1f}  {sig}")


async def _measure(
    label: str,
    *,
    seed: Callable[[int], Awaitable[tuple[Host, list[_SeededDevice]]]],
    run: Callable[[Host, list[_SeededDevice]], Awaitable[None]],
    tap: QueryTap,
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


async def test_bench_node_health_fold(db_session: AsyncSession) -> None:
    service = NodeHealthService(
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        recovery_control=AsyncMock(),
        health=DeviceHealthService(publisher=event_bus),
        incidents=AsyncMock(),
    )
    tap = QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)

    async def _seed(gen: int) -> tuple[Host, list[_SeededDevice]]:
        return await _seed_fleet(db_session, FLEET, DEVICES, generation=gen)

    async def _run(host: Host, devices: list[_SeededDevice]) -> None:
        await service.fold_host_nodes(db_session, host.id, _node_section(devices, CHURN))

    await _measure("fold_host_nodes", seed=_seed, run=_run, tap=tap)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


async def test_bench_device_telemetry_fold(db_session: AsyncSession) -> None:
    service = HardwareTelemetryService(publisher=Mock(), settings=FakeSettingsReader({}))
    tap = QueryTap()
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
    tap = QueryTap()
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
    tap = QueryTap()
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


def test_bench_real_lifecycle_composition() -> None:
    service = _build_real_lifecycle_connectivity_service()

    assert isinstance(service._lifecycle_policy, LifecyclePolicyService)
    assert isinstance(service._health, DeviceHealthService)


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


def _report_whole_push(tap: QueryTap, commits: CommitTap, wall_ms: list[float]) -> None:
    avg = sum(wall_ms) / len(wall_ms)
    q_per_push = tap.total / ITERS
    print(f"\n{'=' * 78}\nwhole_push (all stages): {DEVICES} devices x {ITERS} iters  churn={CHURN}")
    print(f"  wall per push:     avg {avg:.1f} ms   ({', '.join(f'{w:.0f}' for w in wall_ms)})")
    print(f"  QUERIES per push:  {q_per_push:.0f}   ({q_per_push / DEVICES:.2f} per device)")
    print(f"  COMMITS per push:  {commits.count / ITERS:.1f}")
    print("  top statements per push:")
    for sig, n in tap.counter.most_common(18):
        print(f"    {n / ITERS:8.1f}  {sig}")


def _report_device_health_loop(
    tap: QueryTap,
    commits: CommitTap,
    fold_wall_ms: list[float],
    settled_wall_ms: list[float],
) -> None:
    source_queries_per_fold = tap.source_total / ITERS
    deferred_queries_per_fold = tap.deferred_total / ITERS
    complete_queries_per_fold = tap.total / ITERS
    candidate_signatures = (
        "SELECT device_remediation_log",
        "SELECT sessions",
        "SELECT device_intents",
        "SELECT driver_packs",
        "SELECT driver_pack_releases",
        "SELECT driver_pack_platforms",
    )
    candidate_total = sum(tap.counter.get(signature, 0) for signature in candidate_signatures)
    candidate_per_fold = candidate_total / ITERS
    candidate_share = 100.0 * candidate_total / tap.source_total if tap.source_total else 0.0

    print(
        f"\n{'=' * 78}\nfold_host_devices: {DEVICES} devices x {ITERS} iters  churn={CHURN}  lifecycle={LIFECYCLE_MODE}"
    )
    print(
        f"  fold-return wall time:       median {percentile(fold_wall_ms, 0.5):.1f} ms   "
        f"p95 {percentile(fold_wall_ms, 0.95):.1f} ms   ({', '.join(f'{wall:.0f}' for wall in fold_wall_ms)})"
    )
    print(
        f"  event-settled wall time:     median {percentile(settled_wall_ms, 0.5):.1f} ms   "
        f"p95 {percentile(settled_wall_ms, 0.95):.1f} ms   ({', '.join(f'{wall:.0f}' for wall in settled_wall_ms)})"
    )
    print(
        f"  SOURCE queries/fold:         {source_queries_per_fold:.0f}   "
        f"({source_queries_per_fold / DEVICES:.2f} per device)"
    )
    print(f"  DEFERRED event queries/fold: {deferred_queries_per_fold:.0f}")
    print(f"  COMPLETE queries/fold:       {complete_queries_per_fold:.0f}")
    print(f"  SOURCE commits/fold:         {commits.source_count / ITERS:.1f}")
    print(f"  DEFERRED event commits/fold: {commits.deferred_count / ITERS:.1f}")
    print(f"  COMPLETE commits/fold:       {commits.count / ITERS:.1f}")
    print(
        "  candidate batch reads/fold: "
        f"{candidate_per_fold:.0f}   ({candidate_per_fold / DEVICES:.2f} per device, "
        f"{candidate_share:.1f}% of queries)"
    )
    print("  top statements per fold:")
    for signature, count in tap.counter.most_common(18):
        print(f"    {count / ITERS:8.1f}  {signature}")
    print("  top call sites per fold:")
    for (callsite, signature), count in tap.callsite_counter.most_common(24):
        print(f"    {count / ITERS:8.1f}  {callsite}  [{signature}]")
    print("  top call sites per fold by total time (~rows are driver rowcounts, approximate):")
    by_time = sorted(tap.durations.items(), key=lambda kv: sum(kv[1]), reverse=True)
    for (callsite, signature), durations in by_time[:24]:
        calls = tap.callsite_counter[(callsite, signature)]
        print(
            f"    {calls / ITERS:8.1f}  {sum(durations) / ITERS:9.1f}ms  "
            f"med {percentile(durations, 0.5):7.2f}ms  p95 {percentile(durations, 0.95):7.2f}ms  "
            f"~rows {tap.rows[(callsite, signature)] / ITERS:8.1f}  {callsite}  [{signature}]"
        )


async def test_bench_whole_push(db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]) -> None:
    service = _build_push_service(db_session_maker)
    tap = QueryTap()
    commits = CommitTap()
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "commit", commits)
    failures_before = _observation_failure_total()

    tap.armed = False
    commits.armed = False
    host, devices = await _seed_fleet(db_session, FLEET, DEVICES, generation=0)
    await db_session.commit()  # ensure the seed is visible to factory-opened sessions
    wall_ms: list[float] = []
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


async def test_bench_device_health_loop_fold(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_async_session_callsite_profiler(monkeypatch)
    service = _build_device_health_benchmark_service(db_session_maker)
    tap = QueryTap()
    commits = CommitTap()
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "after_cursor_execute", tap.after)
    event.listen(engine, "commit", commits)
    tap.armed = False
    commits.armed = False
    fold_wall_ms: list[float] = []
    settled_wall_ms: list[float] = []

    try:
        host, devices = await _seed_fleet(db_session, FLEET, DEVICES, generation=0)
        for iteration in range(WARMUP + ITERS):
            armed = iteration >= WARMUP
            if CHURN > 0 and iteration > 0:
                host, devices = await _seed_fleet(db_session, FLEET, DEVICES, generation=iteration)

            revision = await next_observation_revision(db_session)
            section = _device_health_loop_section(
                devices,
                CHURN,
                revision=revision,
                section_sequence=iteration + 1,
            )

            tap.armed = armed
            commits.armed = armed
            t0 = perf_counter()
            try:
                settled = await service.fold_host_devices(
                    db_session,
                    host.id,
                    section,
                    boot_id=uuid.uuid4(),
                )
            finally:
                fold_returned_at = perf_counter()
                await settle_after_commit_tasks()
                event_settled_at = perf_counter()
                if armed:
                    fold_wall_ms.append((fold_returned_at - t0) * 1000)
                    settled_wall_ms.append((event_settled_at - t0) * 1000)
                tap.armed = False
                commits.armed = False

            assert settled is True
            receipt_rows = (
                (
                    await db_session.execute(
                        select(Device.device_checks_fold_applied_revision).where(
                            Device.id.in_([device.device_id for device in devices])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(receipt_rows) == len(devices)
            assert set(receipt_rows) == {revision}

        _report_device_health_loop(tap, commits, fold_wall_ms, settled_wall_ms)
        attributed_callsites = {callsite for callsite, _signature_name in tap.callsite_counter}
        assert "unattributed" not in attributed_callsites
        assert "app.devices.locking.lock_device_handle" in attributed_callsites
        if CHURN > 0:
            assert tap.deferred_total > 0
            assert commits.deferred_count > 0
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "after_cursor_execute", tap.after)
        event.remove(engine, "commit", commits)
