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

Set ``FOLD_BENCH_JSON`` to a file path to also write a machine-readable JSON
report of the device-health loop benchmark (see ``build_json_report`` in
tests/bench_instrumentation.py) to that path.

Set ``FOLD_BENCH_EXPLAIN=1`` to capture EXPLAIN plans for the hottest statements
of the device-health loop benchmark (best-effort; a failed plan is reported
inline rather than failing the benchmark).

Set ``FOLD_BENCH_SCENARIO`` to select the churn shape the device-health loop
benchmark drives (default ``steady``):

- ``steady`` -- today's behavior: a churn fraction of devices flip unhealthy
  each iteration, re-seeding a fresh generation only when ``FOLD_BENCH_CHURN``
  is nonzero (``FOLD_BENCH_CHURN`` still controls the fraction).
- ``sparse-unhealthy`` -- exactly one device unhealthy per iteration, fresh
  generation every iteration.
- ``all-unhealthy`` -- every device unhealthy per iteration, fresh generation
  every iteration.
- ``repeat-unhealthy`` -- the same devices stay unhealthy across every
  iteration (no re-seed), so repeated observation of an already-escalated
  device can be measured as the cheap no-op it is expected to be.
- ``stale-ladder`` -- every device carries an active escalation episode (a
  bare failure row) re-armed before every iteration, so the healthy fold's
  self-heal hook takes its residue-clear mutation path every time.
- ``deep-history`` -- every device carries ~200 remediation-log rows ending in
  a reset (episode inactive), so the healthy fold only reads the deep ladder
  without appending to it.
- ``active-claims`` -- the first half of the fleet is claimed (a live session or
  an unexpired verification lease per device), so the fold's busy/verifying
  mask is exercised while still consuming the pushed generation.
- ``terminal-noop`` -- the first half of the fleet is in maintenance and the
  second half is omitted from the pushed section entirely, so both terminal-noop
  paths (maintenance consume, missing-device skip) are measured together
  (maintenance devices are pushed unhealthy so the short-circuit is provable).

The benchmark exercises only facts-backed folds; the asynchronous device-health
fold is measured separately by the StatusFoldLoop benchmark.
"""

from __future__ import annotations

import functools
import json
import os
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, func, select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host
from app.core.metrics_recorders import HOST_PUSH_OBSERVATION_FAILURES
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType
from app.devices.models.intent import DeviceIntent
from app.devices.models.remediation_log import DeviceRemediationLogEntry
from app.devices.services import lifecycle_policy_state
from app.devices.services.connectivity import ConnectivityService
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent_types import CommandKind, verification_intent_source
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.review import ReviewService
from app.hosts.models import Host, HostStatus
from app.hosts.service_hardware_telemetry import HardwareTelemetryService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.service_status_push import OBSERVATION_REVISION_KEY, HostStatusPushService, ObservationFold
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.packs.services.discovery import PackDiscoveryService
from app.runs.service_reservation import RunReservationService
from app.sessions.models import Session, SessionStatus
from tests.bench_instrumentation import (
    CommitTap,
    QueryTap,
    build_json_report,
    explain_statement_sql,
    install_async_session_callsite_profiler,
    percentile,
    select_explain_targets,
)
from tests.fakes import FakeSettingsReader
from tests.helpers import build_connectivity_service, settle_after_commit_tasks
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
JSON_PATH = os.getenv("FOLD_BENCH_JSON")
EXPLAIN = bool(os.getenv("FOLD_BENCH_EXPLAIN"))
SCENARIO = os.getenv("FOLD_BENCH_SCENARIO", "steady")
# repeat-unhealthy needs a nonzero unhealthy fraction even when CHURN is unset.
_REPEAT_CHURN = CHURN if CHURN > 0 else 0.3


@dataclass(frozen=True)
class _HealthScenario:
    """One FOLD_BENCH_SCENARIO shape for the device-health loop benchmark.

    ``seed_extra`` runs once after the fleet seed; ``rearm`` runs unarmed before
    every iteration (so warm-up cannot consume a one-shot mutation path);
    ``verify`` is the fixture-honesty guard -- it must FAIL when the scenario's
    intended code path did not run.
    """

    unhealthy_count: Callable[[int], int]
    reseed_per_iteration: bool
    omit_second_half: bool = False
    seed_extra: Callable[[AsyncSession, list[_SeededDevice]], Awaitable[None]] | None = None
    rearm: Callable[[AsyncSession, list[_SeededDevice]], Awaitable[None]] | None = None
    verify: Callable[[AsyncSession, QueryTap, list[_SeededDevice]], Awaitable[None]] | None = None
    expect_receipts: str = "all"  # "all" | "present-only"


async def _verify_repeat_unhealthy(db: AsyncSession, tap: QueryTap, devices: list[_SeededDevice]) -> None:
    assert all(d.identity.startswith("bench-g0-") for d in devices), (
        "repeat-unhealthy must observe the generation-0 fleet across all iterations; a re-seed occurred"
    )
    k = _churn_count(len(devices), _REPEAT_CHURN)
    unhealthy = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.id.in_([d.device_id for d in devices]), Device.device_checks_healthy.is_(False))
    )
    assert unhealthy == k, f"expected {k} devices to stay unhealthy across repeats, found {unhealthy}"


_DEEP_HISTORY_ROWS = 200


async def _arm_stale_ladders(db: AsyncSession, devices: list[_SeededDevice]) -> None:
    """Give every device an active escalation episode (a bare failure row) so the
    healthy fold's self-heal hook takes its residue-clear mutation path. Used as
    ``rearm`` so the path re-fires every iteration, warm-up included."""
    for d in devices:
        await remediation_log.append_failure(db, d.device_id, source="bench", reason="bench stale residue")
    await db.commit()


async def _verify_stale_ladder_cleared(db: AsyncSession, tap: QueryTap, devices: list[_SeededDevice]) -> None:
    ladder = await remediation_log.load_ladder(db, devices[0].device_id)
    assert ladder.episode_active is False, "self-heal residue clear did not run"
    # The appium-xcuitest test-fixture manifest (tests/packs/fixtures/manifests/appium-xcuitest.yaml)
    # marks "bundle_id" required_for_session for real devices; _seed_fleet never sets it, so those
    # devices are permanently setup_required -> offline and never reach the connectivity healthy
    # path's self-heal branch. Only the appium-uiautomator2 share of the fleet is ready (all of it,
    # under FOLD_BENCH_FLEET=homogeneous), so the reset-append floor is scoped to those devices.
    ready = [d for d in devices if d.spec.pack_id != "appium-xcuitest"]
    assert tap.counter["INSERT device_remediation_log"] >= len(ready) * ITERS, (
        "expected one reset append per ready device per armed iteration"
    )


async def _seed_deep_history(db: AsyncSession, devices: list[_SeededDevice]) -> None:
    """~200 remediation rows per device ending in a reset: episode inactive, so the
    healthy path only READS the deep ladder without appending."""
    base = now_utc() - timedelta(hours=1)
    rows: list[DeviceRemediationLogEntry] = []
    for d in devices:
        for i in range(_DEEP_HISTORY_ROWS - 1):
            failure = i % 2 == 0
            rows.append(
                DeviceRemediationLogEntry(
                    device_id=d.device_id,
                    kind="failure" if failure else "reset",
                    source="bench",
                    action="failure_observed" if failure else "bench_reset",
                    reason="bench deep history",
                    at=base + timedelta(seconds=i),
                )
            )
        rows.append(
            DeviceRemediationLogEntry(
                device_id=d.device_id,
                kind="reset",
                source="bench",
                action="bench_reset",
                reason="bench deep history terminal reset",
                at=base + timedelta(seconds=_DEEP_HISTORY_ROWS),
            )
        )
    db.add_all(rows)
    await db.commit()


async def _verify_deep_history_untouched(db: AsyncSession, tap: QueryTap, devices: list[_SeededDevice]) -> None:
    count = await db.scalar(
        select(func.count())
        .select_from(DeviceRemediationLogEntry)
        .where(DeviceRemediationLogEntry.device_id == devices[0].device_id)
    )
    assert count == _DEEP_HISTORY_ROWS, f"healthy fold must not append to an inactive deep ladder (rows={count})"


async def _seed_active_claims(db: AsyncSession, devices: list[_SeededDevice]) -> None:
    """Claim the first half of the fleet: even claimed indexes get a live session
    (busy mask), odd get an unexpired verification lease (verifying mask)."""
    lease_until = now_utc() + timedelta(hours=1)
    for i, d in enumerate(devices[: len(devices) // 2]):
        if i % 2 == 0:
            db.add(Session(session_id=f"bench-claim-{d.identity}", device_id=d.device_id, status=SessionStatus.running))
        else:
            db.add(
                DeviceIntent(
                    device_id=d.device_id,
                    source=verification_intent_source(d.device_id),
                    kind=CommandKind.verification_start,
                    payload={},
                    expires_at=lease_until,
                )
            )
    await db.commit()


async def _verify_claims_intact(db: AsyncSession, tap: QueryTap, devices: list[_SeededDevice]) -> None:
    claimed = devices[: len(devices) // 2]
    sessions = await db.scalar(
        select(func.count())
        .select_from(Session)
        .where(Session.device_id.in_([d.device_id for d in claimed]), Session.ended_at.is_(None))
    )
    leases = await db.scalar(
        select(func.count()).select_from(DeviceIntent).where(DeviceIntent.device_id.in_([d.device_id for d in claimed]))
    )
    assert sessions == (len(claimed) + 1) // 2, "live session claims disappeared mid-benchmark"
    assert leases == len(claimed) // 2, "verification leases disappeared mid-benchmark"


async def _seed_maintenance_half(db: AsyncSession, devices: list[_SeededDevice]) -> None:
    """First half (the section-present half) goes into maintenance; the second half
    stays out of the pushed section entirely (the missing-device skip)."""
    for d in devices[: len(devices) // 2]:
        device = await device_locking.lock_device(db, d.device_id)
        lifecycle_policy_state.write_state(device, {"maintenance_reason": "bench maintenance"})
        await db.commit()


async def _verify_terminal_noop(db: AsyncSession, tap: QueryTap, devices: list[_SeededDevice]) -> None:
    # Maintenance devices are pushed UNHEALTHY on purpose: the in_maintenance
    # short-circuit precedes health evaluation, so surviving health facts prove
    # the short-circuit fired; the normal path would flip them to False.
    still_healthy = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(
            Device.id.in_([d.device_id for d in devices[: len(devices) // 2]]),
            Device.device_checks_healthy.is_(True),
        )
    )
    assert still_healthy == len(devices) // 2, "maintenance short-circuit must ignore the pushed unhealthy signal"


_SCENARIOS: dict[str, _HealthScenario] = {
    "steady": _HealthScenario(
        unhealthy_count=lambda n: _churn_count(n, CHURN),
        reseed_per_iteration=CHURN > 0,
    ),
    "sparse-unhealthy": _HealthScenario(unhealthy_count=lambda n: 1, reseed_per_iteration=True),
    "all-unhealthy": _HealthScenario(unhealthy_count=lambda n: n, reseed_per_iteration=True),
    "repeat-unhealthy": _HealthScenario(
        unhealthy_count=lambda n: _churn_count(n, _REPEAT_CHURN),
        reseed_per_iteration=False,
        verify=_verify_repeat_unhealthy,
    ),
    "stale-ladder": _HealthScenario(
        unhealthy_count=lambda n: 0,
        reseed_per_iteration=False,
        rearm=_arm_stale_ladders,
        verify=_verify_stale_ladder_cleared,
    ),
    "deep-history": _HealthScenario(
        unhealthy_count=lambda n: 0,
        reseed_per_iteration=False,
        seed_extra=_seed_deep_history,
        verify=_verify_deep_history_untouched,
    ),
    "active-claims": _HealthScenario(
        unhealthy_count=lambda n: 0,
        reseed_per_iteration=False,
        seed_extra=_seed_active_claims,
        verify=_verify_claims_intact,
    ),
    "terminal-noop": _HealthScenario(
        unhealthy_count=lambda n: n,
        reseed_per_iteration=False,
        omit_second_half=True,
        seed_extra=_seed_maintenance_half,
        verify=_verify_terminal_noop,
        expect_receipts="present-only",
    ),
}
if SCENARIO not in _SCENARIOS:
    raise ValueError(f"unknown FOLD_BENCH_SCENARIO {SCENARIO!r}; known: {sorted(_SCENARIOS)}")


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


async def _explain_top_statements(engine: AsyncEngine, tap: QueryTap) -> list[dict[str, str]]:
    """Best-effort plans for the hottest statements. Runs unarmed on a fresh
    connection and rolls back. A failed plan (parameter-shape mismatch, etc.)
    is reported inline, never raised — this is diagnostics, not correctness."""
    plans: list[dict[str, str]] = []
    async with engine.connect() as conn:
        for (callsite, signature), statement, parameters in select_explain_targets(tap):
            sql = explain_statement_sql(statement)
            mode = "analyze" if sql.startswith("EXPLAIN (") else "plain"
            try:
                result = await conn.exec_driver_sql(sql, parameters or ())
                plan = "\n".join(str(row[0]) for row in result)
            except Exception as exc:  # noqa: BLE001 - diagnostics must not fail the bench
                plan = f"EXPLAIN failed: {exc!r}"
                await conn.rollback()
            plans.append({"callsite": callsite, "signature": signature, "mode": mode, "plan": plan})
        await conn.rollback()
    return plans


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
    scenario = _SCENARIOS[SCENARIO]
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
        if scenario.seed_extra is not None:
            await scenario.seed_extra(db_session, devices)
        for iteration in range(WARMUP + ITERS):
            armed = iteration >= WARMUP
            if scenario.reseed_per_iteration and iteration > 0:
                host, devices = await _seed_fleet(db_session, FLEET, DEVICES, generation=iteration)
                if scenario.seed_extra is not None:
                    await scenario.seed_extra(db_session, devices)
            if scenario.rearm is not None:
                await scenario.rearm(db_session, devices)

            present = devices[: len(devices) // 2] if scenario.omit_second_half else devices
            revision = await next_observation_revision(db_session)
            section = _device_health_loop_section(
                present,
                unhealthy_count=scenario.unhealthy_count(len(present)),
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
            present_ids = [device.device_id for device in present]
            receipt_rows = (
                (
                    await db_session.execute(
                        select(Device.device_checks_fold_applied_revision).where(Device.id.in_(present_ids))
                    )
                )
                .scalars()
                .all()
            )
            assert len(receipt_rows) == len(present_ids)
            assert set(receipt_rows) == {revision}
            if scenario.expect_receipts == "present-only":
                omitted_ids = [device.device_id for device in devices[len(devices) // 2 :]]
                stale_rows = (
                    (
                        await db_session.execute(
                            select(Device.device_checks_fold_applied_revision).where(Device.id.in_(omitted_ids))
                        )
                    )
                    .scalars()
                    .all()
                )
                assert all(row < revision for row in stale_rows), "omitted devices must not advance receipts"

        explain_plans: list[dict[str, str]] = []
        if EXPLAIN:
            explain_plans = await _explain_top_statements(db_session.bind, tap)
            print("  query plans (top call sites by total time):")
            for entry in explain_plans:
                print(f"    -- {entry['callsite']}  [{entry['signature']}]  ({entry['mode']})")
                for line in entry["plan"].splitlines():
                    print(f"       {line}")

        _report_device_health_loop(tap, commits, fold_wall_ms, settled_wall_ms)
        if JSON_PATH:
            report = build_json_report(
                config={
                    "scenario": SCENARIO,
                    "devices": DEVICES,
                    "iters": ITERS,
                    "warmup": WARMUP,
                    "churn": CHURN,
                    "fleet": os.getenv("FOLD_BENCH_FLEET", "mixed"),
                    "lifecycle": LIFECYCLE_MODE,
                },
                tap=tap,
                commits=commits,
                iters=ITERS,
                fold_wall_ms=fold_wall_ms,
                settled_wall_ms=settled_wall_ms,
                explain_plans=explain_plans,
            )
            Path(JSON_PATH).write_text(json.dumps(report, indent=2))
        attributed_callsites = {callsite for callsite, _signature_name in tap.callsite_counter}
        assert "unattributed" not in attributed_callsites
        assert "app.devices.locking.lock_device_handle" in attributed_callsites
        # Gated on reseed_per_iteration, not just effective_unhealthy > 0: a device
        # already offline is never re-escalated (connectivity._escalate_health_failure
        # skips handle_health_failure once was_offline), so a static scenario that
        # never re-seeds (e.g. repeat-unhealthy) has its one real transition land in
        # the unarmed warm-up iteration and legitimately shows zero deferred queries
        # in the armed window -- that non-reseeding no-op is exactly what such a
        # scenario measures. scenario.verify is the honesty guard for that case.
        effective_unhealthy = scenario.unhealthy_count(len(devices) // 2 if scenario.omit_second_half else len(devices))
        if effective_unhealthy > 0 and scenario.reseed_per_iteration:
            assert tap.deferred_total > 0
            assert commits.deferred_count > 0
        if scenario.verify is not None:
            await scenario.verify(db_session, tap, devices)
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "after_cursor_execute", tap.after)
        event.remove(engine, "commit", commits)
