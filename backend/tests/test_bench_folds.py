"""In-process load benchmark for the status-push folds.

Reproduces the per-push CPU cost of the synchronous status-push folds and the
node-health fold against a synthetic fleet, so fold optimizations can be
measured deterministically with cProfile instead of prod py-spy sampling.

Only ``test_bench_healthy_fold_statement_budget`` runs in CI. Everything else here
is opt-in behind ``FOLD_BENCH=1`` and its assertions have no standing regression
value -- including the deep-history ladder pins, whose scenario needs the
``FOLD_BENCH_SCENARIO``/``FOLD_BENCH_FLEET`` harness and cannot be reached from a
plain pytest run. Deliberate: reproducing that scenario as a standing test means
re-seeding ~200 remediation rows per device on every suite run. Verify it by hand
after touching the snapshot loader, with the command in
``_verify_deep_history_untouched``.

The load benchmarks are skipped in the normal suite. Run explicitly:

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
- ``stale-run-exclusion`` -- every device is reserved by one non-terminal run
  with an indefinite health-failure exclusion, re-armed before every iteration,
  so the healthy fold's self-heal hook takes its run-restore mutation path.

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
from sqlalchemy import event, func, select, update

from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.node_health import NodeHealthService
from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host
from app.core.metrics_recorders import HOST_PUSH_OBSERVATION_FAILURES
from app.core.observation_revision import next_observation_revision
from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceOperationalState
from app.devices.models.intent import DeviceIntent
from app.devices.models.remediation_log import DeviceRemediationLogEntry
from app.devices.models.reservation import DeviceReservation, ExclusionKind
from app.devices.services import lifecycle_policy_state
from app.devices.services.health import DeviceHealthService
from app.devices.services.intent_types import CommandKind, verification_intent_source
from app.devices.services.property_refresh import PropertyRefreshService
from app.devices.services.state import derive_operational_state
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.service_status_push import HostStatusPushService, ObservationFold, StatusPushTarget
from app.lifecycle.services import remediation_log
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.packs.services.discovery import PackDiscoveryService
from app.runs.models import RunState, TestRun
from app.sessions.models import Session, SessionStatus
from tests.bench_instrumentation import (
    CommitTap,
    QueryTap,
    assert_fold_boundary_shape,
    build_json_report,
    explain_statement_sql,
    install_async_session_callsite_profiler,
    percentile,
    scenario_observation_shape,
    select_explain_targets,
    validate_benchmark_knobs,
)
from tests.fakes import FakeSettingsReader
from tests.fold_fixtures import (
    HOMOGENEOUS_FLEET,
    MIXED_FLEET,
    SeededDevice,
    TupleSpec,
    build_real_lifecycle_connectivity_service,
    device_health_loop_section,
    seed_fleet,
)
from tests.helpers import build_connectivity_service, dispatch_committed_events
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from app.devices.services.connectivity import ConnectivityService
    from app.hosts.models import Host

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("seeded_driver_packs"),
]

# The load benchmarks below drive a real fleet under the FOLD_BENCH_* knobs and
# print a report; they are opt-in. ``test_bench_healthy_fold_statement_budget``
# deliberately carries no such mark: it is a fixed 10-device fold that reads none
# of those knobs, and behind this skip its statement-category pin had exactly zero
# standing regression value -- the state W3.1 of the Phase 11 follow-ups names.
bench_enabled = bool(os.getenv("FOLD_BENCH"))
bench_only = pytest.mark.skipif(not bench_enabled, reason="set FOLD_BENCH=1 to run the fold load benchmark")

if bench_enabled:
    DEVICES = int(os.getenv("FOLD_BENCH_DEVICES", "50"))
    ITERS = int(os.getenv("FOLD_BENCH_ITERS", "3"))
    WARMUP = int(os.getenv("FOLD_BENCH_WARMUP", "1"))
    CHURN = float(os.getenv("FOLD_BENCH_CHURN", "0.0"))
    validate_benchmark_knobs(devices=DEVICES, iters=ITERS, warmup=WARMUP, churn=CHURN)
    _raw_lifecycle_mode = os.getenv("FOLD_BENCH_LIFECYCLE", "real")
    if _raw_lifecycle_mode not in ("real", "isolated"):
        raise ValueError("FOLD_BENCH_LIFECYCLE must be 'real' or 'isolated'")
    LIFECYCLE_MODE = cast("Literal['real', 'isolated']", _raw_lifecycle_mode)
    JSON_PATH = os.getenv("FOLD_BENCH_JSON")
    EXPLAIN = bool(os.getenv("FOLD_BENCH_EXPLAIN"))
    SCENARIO = os.getenv("FOLD_BENCH_SCENARIO", "steady")
    FLEET: tuple[TupleSpec, ...] = (
        HOMOGENEOUS_FLEET if os.getenv("FOLD_BENCH_FLEET", "mixed") == "homogeneous" else MIXED_FLEET
    )
else:
    DEVICES = 50
    ITERS = 3
    WARMUP = 1
    CHURN = 0.0
    LIFECYCLE_MODE: Literal["real", "isolated"] = "real"
    JSON_PATH = None
    EXPLAIN = False
    SCENARIO = "steady"
    FLEET = MIXED_FLEET


@dataclass(frozen=True)
class _HealthScenario:
    """One FOLD_BENCH_SCENARIO shape for the device-health loop benchmark.

    ``seed_extra`` runs once after the fleet seed; ``rearm`` runs unarmed before
    every iteration (so warm-up cannot consume a one-shot mutation path);
    ``verify`` is the fixture-honesty guard -- it must FAIL when the scenario's
    intended code path did not run.
    """

    reseed_per_iteration: bool
    seed_extra: Callable[[AsyncSession, list[SeededDevice]], Awaitable[None]] | None = None
    rearm: Callable[[AsyncSession, list[SeededDevice]], Awaitable[None]] | None = None
    verify: Callable[[AsyncSession, QueryTap, list[SeededDevice]], Awaitable[None]] | None = None
    expect_receipts: str = "all"  # "all" | "present-only"


async def _verify_repeat_unhealthy(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
    assert all(d.identity.startswith("bench-g0-") for d in devices), (
        "repeat-unhealthy must observe the generation-0 fleet across all iterations; a re-seed occurred"
    )
    shape = scenario_observation_shape(scenario="repeat-unhealthy", devices=len(devices), churn=CHURN)
    unhealthy = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.id.in_([d.device_id for d in devices]), Device.device_checks_healthy.is_(False))
    )
    assert unhealthy == shape.unhealthy_count, (
        f"expected {shape.unhealthy_count} devices to stay unhealthy across repeats, found {unhealthy}"
    )


async def _verify_unhealthy_cardinality(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
    shape = scenario_observation_shape(scenario=SCENARIO, devices=len(devices), churn=CHURN)
    unhealthy = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.id.in_([d.device_id for d in devices]), Device.device_checks_healthy.is_(False))
    )
    assert unhealthy == shape.unhealthy_count, (
        f"{SCENARIO} expected exactly {shape.unhealthy_count} unhealthy devices, found {unhealthy}"
    )


_DEEP_HISTORY_ROWS = 200


async def _arm_stale_ladders(db: AsyncSession, devices: list[SeededDevice]) -> None:
    """Give every device an active escalation episode (a bare failure row) so the
    healthy fold's self-heal hook takes its residue-clear mutation path. Used as
    ``rearm`` so the path re-fires every iteration, warm-up included."""
    for d in devices:
        await remediation_log.append_failure(db, d.device_id, source="bench", reason="bench stale residue")
    await db.commit()


async def _verify_stale_ladder_cleared(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
    ladder = await remediation_log.load_ladder(db, devices[0].device_id)
    assert ladder.episode_active is False, "self-heal residue clear did not run"
    # The appium-xcuitest test-fixture manifest (tests/packs/fixtures/manifests/appium-xcuitest.yaml)
    # marks "bundle_id" required_for_session for real devices; seed_fleet never sets it, so those
    # devices are permanently setup_required -> offline and never reach the connectivity healthy
    # path's self-heal branch. Only the appium-uiautomator2 share of the fleet is ready (all of it,
    # under FOLD_BENCH_FLEET=homogeneous), so the reset-append floor is scoped to those devices.
    ready = [d for d in devices if d.spec.pack_id != "appium-xcuitest"]
    assert tap.counter["INSERT device_remediation_log"] >= len(ready) * ITERS, (
        "expected one reset append per ready device per armed iteration"
    )


async def _seed_deep_history(db: AsyncSession, devices: list[SeededDevice]) -> None:
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


async def _verify_deep_history_untouched(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
    count = await db.scalar(
        select(func.count())
        .select_from(DeviceRemediationLogEntry)
        .where(DeviceRemediationLogEntry.device_id == devices[0].device_id)
    )
    assert count == _DEEP_HISTORY_ROWS, f"healthy fold must not append to an inactive deep ladder (rows={count})"
    ladder_key = ("app.devices.services.decision_snapshot._load_current_ladder", "SELECT device_remediation_log")
    reads = tap.callsite_counter[ladder_key]
    rows = tap.rows[ladder_key]
    expected_reads = len(devices) * ITERS
    # Two separate properties, each with its own message, because one number was
    # previously asserted twice: a ``>=`` on the read count three lines above an
    # ``==`` on the row count, both against ``len(devices) * ITERS``. A read that
    # ran twice per device passed the loose one and failed the exact one with a
    # message blaming history scanning -- the wrong diagnosis for the wrong number.
    assert reads == expected_reads, (
        f"the ladder read ran {reads} times, expected exactly {expected_reads} "
        f"(once per device per armed iteration); a larger number is a repeated read per device, "
        f"a smaller one means it stopped running per device"
    )
    # The seed ends in a terminal reset and the bounded read's predicate is
    # ``(at, id) >= (reset.at, reset.id)``, so each read returns exactly the reset
    # row itself and nothing behind it. Rows-per-read, not a total, so this cannot
    # be satisfied by the read count moving. The former bound was
    # ``< len(devices) * _DEEP_HISTORY_ROWS * ITERS``, roughly 200x looser, so a
    # read that regressed to scanning 90% of the history still passed.
    # Re-measure with:
    #   FOLD_BENCH=1 FOLD_BENCH_SCENARIO=deep-history FOLD_BENCH_DEVICES=4 \
    #   FOLD_BENCH_ITERS=2 FOLD_BENCH_WARMUP=0 FOLD_BENCH_FLEET=homogeneous \
    #   uv run pytest -q -s tests/test_bench_folds.py::test_bench_device_health_loop_fold
    assert rows == reads, (
        f"the ladder read returned {rows} rows across {reads} reads, expected exactly one row per read "
        f"(the terminal reset). More means the read regressed to scanning history behind the reset."
    )


async def _seed_active_claims(db: AsyncSession, devices: list[SeededDevice]) -> None:
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


async def _verify_claims_intact(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
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
    # Phase 3 folded the standalone ``device_has_masking_live_session`` and
    # ``device_has_verification_lease`` predicates into one combined
    # claims/intents/reservation select per locked device, so that is the call
    # site the claim reads now land on -- see the categories asserted in
    # test_bench_healthy_fold_statement_budget. Naming the retired predicates
    # here made this scenario assert against a read the fold no longer issues.
    snapshot_key = (
        "app.devices.services.decision_snapshot._load_claims_intents_and_reservation",
        "SELECT device_intents",
    )
    assert tap.callsite_counter[snapshot_key] >= len(devices) * ITERS, "claim snapshot read did not run per device"
    assert tap.rows[snapshot_key] >= (len(claimed) // 2) * ITERS, "claim snapshot read missed the seeded claims"
    # Over the whole fleet, not the claimed half: the snapshot read runs once per
    # locked device, so every present device's id is in the parameter set and a
    # loop over the claimed subset alone could not fail. This is the property the
    # parameter capture can actually prove.
    snapshot_parameters = tap.captured_parameter_values(snapshot_key)
    unseen = sorted(d.identity for d in devices if str(d.device_id) not in snapshot_parameters)
    assert unseen == [], f"claim snapshot did not inspect {unseen}"
    for index, seeded in enumerate(claimed):
        device = await db.get(Device, seeded.device_id)
        assert device is not None
        state = await derive_operational_state(db, device, now=now_utc())
        expected = DeviceOperationalState.busy if index % 2 == 0 else DeviceOperationalState.verifying
        assert state == expected, f"claim for {seeded.identity} projected {state}, expected {expected}"


async def _seed_maintenance_half(db: AsyncSession, devices: list[SeededDevice]) -> None:
    """First half (the section-present half) goes into maintenance; the second half
    stays out of the pushed section entirely (the missing-device skip)."""
    for d in devices[: len(devices) // 2]:
        device = await device_locking.lock_device(db, d.device_id)
        lifecycle_policy_state.write_state(device, {"maintenance_reason": "bench maintenance"})
        await db.commit()


async def _verify_terminal_noop(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
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


async def _seed_stale_run_exclusions(db: AsyncSession, devices: list[SeededDevice]) -> None:
    """One non-terminal run reserving every device, each reservation carrying an
    indefinite health exclusion — the state restore_run_after_self_heal clears."""
    run = TestRun(name="bench-exclusion-run", state=RunState.active, requirements=[])
    db.add(run)
    await db.flush()
    now = now_utc()
    for d in devices:
        db.add(
            DeviceReservation(
                run_id=run.id,
                device_id=d.device_id,
                identity_value=d.identity,
                connection_target=d.identity,
                pack_id=d.spec.pack_id,
                platform_id=d.spec.platform_id,
                os_version=d.spec.os_version,
                excluded=True,
                exclusion_kind=ExclusionKind.exclusion.value,
                exclusion_reason="bench health-failure exclusion",
                excluded_at=now,
                excluded_until=None,
            )
        )
    await db.commit()


async def _rearm_run_exclusions(db: AsyncSession, devices: list[SeededDevice]) -> None:
    await db.execute(
        update(DeviceReservation)
        .where(
            DeviceReservation.device_id.in_([d.device_id for d in devices]),
            DeviceReservation.released_at.is_(None),
        )
        .values(
            excluded=True,
            exclusion_kind=ExclusionKind.exclusion.value,
            exclusion_reason="bench health-failure exclusion",
            excluded_at=now_utc(),
            excluded_until=None,
        )
    )
    await db.commit()


async def _verify_run_exclusion_restored(db: AsyncSession, tap: QueryTap, devices: list[SeededDevice]) -> None:
    # The appium-xcuitest test-fixture manifest (tests/packs/fixtures/manifests/appium-xcuitest.yaml)
    # marks "bundle_id" required_for_session for real devices; seed_fleet never sets it, so those
    # devices are permanently setup_required -> offline and never reach the connectivity healthy
    # path's self-heal branch (_maybe_auto_recover only calls reconcile_self_heal_locked when
    # operational_state != offline). Only the appium-uiautomator2 share of the fleet is ready (all of
    # it, under FOLD_BENCH_FLEET=homogeneous), so the restored-exclusion floor is scoped to those
    # devices, mirroring _verify_stale_ladder_cleared.
    ready = [d for d in devices if d.spec.pack_id != "appium-xcuitest"]
    still_excluded = await db.scalar(
        select(func.count())
        .select_from(DeviceReservation)
        .where(
            DeviceReservation.device_id.in_([d.device_id for d in ready]),
            DeviceReservation.released_at.is_(None),
            DeviceReservation.excluded.is_(True),
        )
    )
    assert still_excluded == 0, f"restore_run_after_self_heal did not clear {still_excluded} exclusions"


_SCENARIOS: dict[str, _HealthScenario] = {
    "steady": _HealthScenario(
        reseed_per_iteration=CHURN > 0,
    ),
    "sparse-unhealthy": _HealthScenario(reseed_per_iteration=True, verify=_verify_unhealthy_cardinality),
    "all-unhealthy": _HealthScenario(reseed_per_iteration=True, verify=_verify_unhealthy_cardinality),
    "repeat-unhealthy": _HealthScenario(
        reseed_per_iteration=False,
        verify=_verify_repeat_unhealthy,
    ),
    "stale-ladder": _HealthScenario(
        reseed_per_iteration=False,
        rearm=_arm_stale_ladders,
        verify=_verify_stale_ladder_cleared,
    ),
    "deep-history": _HealthScenario(
        reseed_per_iteration=False,
        seed_extra=_seed_deep_history,
        verify=_verify_deep_history_untouched,
    ),
    "active-claims": _HealthScenario(
        reseed_per_iteration=False,
        seed_extra=_seed_active_claims,
        verify=_verify_claims_intact,
    ),
    "terminal-noop": _HealthScenario(
        reseed_per_iteration=False,
        seed_extra=_seed_maintenance_half,
        verify=_verify_terminal_noop,
        expect_receipts="present-only",
    ),
    "stale-run-exclusion": _HealthScenario(
        reseed_per_iteration=False,
        seed_extra=_seed_stale_run_exclusions,
        rearm=_rearm_run_exclusions,
        verify=_verify_run_exclusion_restored,
    ),
}
if SCENARIO not in _SCENARIOS:
    raise ValueError(f"unknown FOLD_BENCH_SCENARIO {SCENARIO!r}; known: {sorted(_SCENARIOS)}")


def _build_device_health_benchmark_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> ConnectivityService:
    if LIFECYCLE_MODE == "isolated":
        return build_connectivity_service(session_factory)
    return build_real_lifecycle_connectivity_service()


def _churn_count(n: int, churn: float) -> int:
    """First-k selection size for a churn fraction (deterministic, index-based)."""
    return round(n * churn)


def _device_section(devices: list[SeededDevice], churn: float = 0.0) -> dict[str, object]:
    k = _churn_count(len(devices), churn)
    return {
        "reported_at": now_utc().isoformat(),
        "devices": {
            d.identity: {"healthy": i >= k}  # first k unhealthy
            for i, d in enumerate(devices)
        },
    }


def _node_section(devices: list[SeededDevice], churn: float = 0.0) -> dict[str, object]:
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


def _properties_section(devices: list[SeededDevice], churn: float = 0.0) -> dict[str, object]:
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
    seed: Callable[[int], Awaitable[tuple[Host, list[SeededDevice]]]],
    run: Callable[[Host, list[SeededDevice]], Awaitable[None]],
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


@bench_only
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

    async def _seed(gen: int) -> tuple[Host, list[SeededDevice]]:
        return await seed_fleet(db_session, FLEET, DEVICES, generation=gen)

    async def _run(host: Host, devices: list[SeededDevice]) -> None:
        await service.fold_host_nodes(db_session, host.id, _node_section(devices, CHURN))

    await _measure("fold_host_nodes", seed=_seed, run=_run, tap=tap)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


@bench_only
async def test_bench_device_properties_fold(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    discovery = PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(),
        circuit_breaker=Mock(),
        serializer=Mock(),
        identity_guard=AsyncMock(),
    )
    service = PropertyRefreshService(discovery=discovery)
    tap = QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)

    async def _seed(gen: int) -> tuple[Host, list[SeededDevice]]:
        host, devices = await seed_fleet(db_session, FLEET, DEVICES, generation=gen)
        await db_session.commit()  # ensure the seed is visible to factory-opened sessions
        return host, devices

    async def _run(host: Host, devices: list[SeededDevice]) -> None:
        await service.fold_host_device_properties(db_session_maker, host.id, _properties_section(devices, CHURN))

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


@bench_only
async def test_bench_host_telemetry_fold(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession]
) -> None:
    service = HostResourceTelemetryService(settings=FakeSettingsReader({}))
    tap = QueryTap()
    event.listen(db_session.bind.sync_engine, "before_cursor_execute", tap)
    tap.armed = False  # exclude the one-time seed from the per-push query count
    host, _devices = await seed_fleet(db_session, FLEET, DEVICES)
    await db_session.commit()  # ensure the seed is visible to factory-opened sessions
    wall_ms: list[float] = []
    for iteration in range(ITERS):
        tap.armed = True
        t0 = perf_counter()
        await service.fold_host_telemetry(db_session_maker, host.id, _host_telemetry_sample(iteration))
        wall_ms.append((perf_counter() - t0) * 1000)
        tap.armed = False
    _report("fold_host_telemetry", tap, wall_ms)
    event.remove(db_session.bind.sync_engine, "before_cursor_execute", tap)


@bench_only
def test_bench_real_lifecycle_composition() -> None:
    service = build_real_lifecycle_connectivity_service()

    assert isinstance(service._lifecycle_policy, LifecyclePolicyService)
    assert isinstance(service._health, DeviceHealthService)


def _observation_failure_total() -> float:
    """Sum every child counter of HOST_PUSH_OBSERVATION_FAILURES. process_observations
    swallows per-stage exceptions and bumps this; a stubbing gap would silently skip a
    stage and undercount, so the whole-push bench asserts this does not rise.

    Catches STAGE-level failures only: restart ingest, convergence, and each fold that
    raises out of process_observations (including the dial-seam-bearing device_health
    fold). It does NOT catch per-device/per-section errors that the properties and
    host_telemetry folds swallow internally within their own session boundaries +
    logger.exception -- acceptable here because those two folds have no agent-dial
    seams for a stubbing gap to break."""
    return sum(
        sample.value
        for metric in HOST_PUSH_OBSERVATION_FAILURES.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    )


def _build_push_service(session_factory: async_sessionmaker[AsyncSession]) -> HostStatusPushService:
    settings = FakeSettingsReader({})
    discovery = PackDiscoveryService(
        agent_get_pack_devices=AsyncMock(), circuit_breaker=Mock(), serializer=Mock(), identity_guard=AsyncMock()
    )
    property_refresh = PropertyRefreshService(discovery=discovery)
    resource_telemetry = HostResourceTelemetryService(settings=settings)
    reconciler = ReconcilerService(
        publisher=event_bus,
        settings=settings,
        pool=None,
        circuit_breaker=Mock(),
        session_factory=session_factory,
        incidents=LifecycleIncidentService(),
    )
    heartbeat = HeartbeatService(
        publisher=event_bus, settings=settings, pool=Mock(), circuit_breaker=Mock(), session_factory=session_factory
    )
    return HostStatusPushService(
        publisher=event_bus,
        session_factory=session_factory,
        # node_health moved to the StatusFoldLoop; it is folded off the request
        # path there, matching production wiring (app/composition.py).
        observation_folds=(
            ObservationFold("device_properties", property_refresh.fold_host_device_properties),
            ObservationFold("host_telemetry", resource_telemetry.fold_host_telemetry),
        ),
        converge_host=functools.partial(converge_pushed_host, session_factory=session_factory, reconciler=reconciler),
        ingest_restart_events=heartbeat.ingest_restart_events,
    )


def _consolidated_payload(devices: list[SeededDevice], churn: float, iteration: int) -> dict[str, object]:
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
    poll_delivery_wall_ms: list[float],
) -> None:
    source_queries_per_fold = tap.total / ITERS
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
    candidate_share = 100.0 * candidate_total / tap.total if tap.total else 0.0

    print(
        f"\n{'=' * 78}\nfold_host_devices: {DEVICES} devices x {ITERS} iters  churn={CHURN}  lifecycle={LIFECYCLE_MODE}"
    )
    print(
        f"  fold-return wall time:       median {percentile(fold_wall_ms, 0.5):.1f} ms   "
        f"p95 {percentile(fold_wall_ms, 0.95):.1f} ms   ({', '.join(f'{wall:.0f}' for wall in fold_wall_ms)})"
    )
    print(
        f"  poller round-trip wall time: median {percentile(poll_delivery_wall_ms, 0.5):.1f} ms   "
        f"p95 {percentile(poll_delivery_wall_ms, 0.95):.1f} ms   "
        f"({', '.join(f'{wall:.0f}' for wall in poll_delivery_wall_ms)})"
    )
    print(
        f"  SOURCE queries/fold:         {source_queries_per_fold:.0f}   "
        f"({source_queries_per_fold / DEVICES:.2f} per device)"
    )
    print(f"  COMPLETE queries/fold:       {complete_queries_per_fold:.0f}")
    print(f"  SOURCE commits/fold:         {commits.count / ITERS:.1f}")
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


@bench_only
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
    host, devices = await seed_fleet(db_session, FLEET, DEVICES, generation=0)
    await db_session.commit()  # ensure the seed is visible to factory-opened sessions
    wall_ms: list[float] = []
    for iteration in range(ITERS):
        if CHURN > 0 and iteration > 0:
            host, devices = await seed_fleet(db_session, FLEET, DEVICES, generation=iteration)
            await db_session.commit()
        payload = _consolidated_payload(devices, CHURN, iteration)
        tap.armed = True
        commits.armed = True
        t0 = perf_counter()
        await service.process_observations(target=StatusPushTarget(host.id, host.ip, host.agent_port), payload=payload)
        wall_ms.append((perf_counter() - t0) * 1000)
        tap.armed = False
        commits.armed = False

    event.remove(engine, "before_cursor_execute", tap)
    event.remove(engine, "commit", commits)
    _report_whole_push(tap, commits, wall_ms)
    # Guard: a stubbing gap would make process_observations silently skip a stage.
    assert _observation_failure_total() == failures_before, "a whole-push stage failed (check dial stubs / wiring)"


@bench_only
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
    poll_delivery_wall_ms: list[float] = []

    try:
        host, devices = await seed_fleet(db_session, FLEET, DEVICES, generation=0)
        if scenario.seed_extra is not None:
            await scenario.seed_extra(db_session, devices)
        for iteration in range(WARMUP + ITERS):
            armed = iteration >= WARMUP
            if scenario.reseed_per_iteration and iteration > 0:
                host, devices = await seed_fleet(db_session, FLEET, DEVICES, generation=iteration)
                if scenario.seed_extra is not None:
                    await scenario.seed_extra(db_session, devices)
            if scenario.rearm is not None:
                await scenario.rearm(db_session, devices)

            shape = scenario_observation_shape(scenario=SCENARIO, devices=len(devices), churn=CHURN)
            present = devices[: shape.present_count]
            revision = await next_observation_revision(db_session)
            section = device_health_loop_section(
                present,
                unhealthy_count=shape.unhealthy_count,
                revision=revision,
                section_sequence=iteration + 1,
            )
            # Publish the seed/rearm before arming: the fold opens transactions of
            # its own on this session (Phase 10 gave the inventory read its own
            # ``db.begin()``) and cannot nest into the seed's implicit one. Same
            # reason as tests/events/test_outbox_commit_budget.py. Unarmed, so the
            # seed's commit is never counted as fold cost.
            await db_session.commit()

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
                await dispatch_committed_events()
                poll_delivered_at = perf_counter()
                if armed:
                    fold_wall_ms.append((fold_returned_at - t0) * 1000)
                    poll_delivery_wall_ms.append((poll_delivered_at - t0) * 1000)
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
                omitted_ids = [device.device_id for device in devices[shape.present_count :]]
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

        _report_device_health_loop(tap, commits, fold_wall_ms, poll_delivery_wall_ms)
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
                poll_delivery_wall_ms=poll_delivery_wall_ms,
                explain_plans=explain_plans,
            )
            Path(JSON_PATH).write_text(json.dumps(report, indent=2))
        attributed_callsites = {callsite for callsite, _signature_name in tap.callsite_counter}
        assert "unattributed" not in attributed_callsites
        assert "app.devices.locking.lock_device_handle" in attributed_callsites
        # Phase 10 boundary shape for this cell: no event-owned transaction, and
        # one source transaction per present device plus the inventory read.
        assert_fold_boundary_shape(
            tap=tap,
            commits=commits,
            scenario=SCENARIO,
            devices=len(devices),
            iters=ITERS,
            churn=CHURN,
        )
        # Gated on reseed_per_iteration, not just effective_unhealthy > 0: a device
        # already offline is never re-escalated (connectivity._escalate_health_failure
        # skips handle_health_failure once was_offline), so a static scenario that
        # never re-seeds (e.g. repeat-unhealthy) has its one real transition land in
        # the unarmed warm-up iteration and legitimately emits no event in the armed
        # window -- that non-reseeding no-op is exactly what such a scenario
        # measures. scenario.verify is the honesty guard for that case.
        effective_unhealthy = scenario_observation_shape(
            scenario=SCENARIO,
            devices=len(devices),
            churn=CHURN,
        ).unhealthy_count
        if effective_unhealthy > 0 and scenario.reseed_per_iteration:
            assert tap.counter["INSERT system_events"] > 0
        if scenario.verify is not None:
            await scenario.verify(db_session, tap, devices)
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "after_cursor_execute", tap.after)
        event.remove(engine, "commit", commits)


async def test_bench_healthy_fold_statement_budget(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Statement-category budget for a 10-device healthy real-lifecycle fold.

    Phase 3 folds each device's claims/reservation and remediation ladder into
    the locked decision snapshot, so a healthy fold issues exactly one device
    lock, one combined claims/intents/reservation select, one ladder select, and
    one commit per device -- and zero of the standalone ladder / reservation
    reads Phase 3 removed. Self-contained (fixed 10-device healthy fold) so it is
    independent of the FOLD_BENCH_* size/scenario knobs.
    """
    install_async_session_callsite_profiler(monkeypatch)
    device_count = 10
    service = build_real_lifecycle_connectivity_service()
    tap = QueryTap()
    commits = CommitTap()
    engine = db_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "commit", commits)
    tap.armed = False
    commits.armed = False
    try:
        host, devices = await seed_fleet(db_session, MIXED_FLEET, device_count, generation=0)
        revision = await next_observation_revision(db_session)
        section = device_health_loop_section(
            devices,
            unhealthy_count=0,
            revision=revision,
            section_sequence=1,
        )
        # See the note in test_bench_device_health_loop_fold: the fold's inventory
        # read owns a transaction, so the seed's implicit one has to end first.
        await db_session.commit()
        tap.armed = True
        commits.armed = True
        settled = await service.fold_host_devices(db_session, host.id, section, boot_id=uuid.uuid4())
        tap.armed = False
        commits.armed = False
        await dispatch_committed_events()
        assert settled is True
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "commit", commits)

    # A statement the profiler cannot attribute to a known callsite surfaces as
    # "unattributed". Guard against a new per-device N+1 against an untracked table
    # (appium_nodes / hosts / sessions / packs) slipping past the named categories,
    # whose strict counts would not move for a query outside those six buckets.
    attributed_callsites = {callsite for callsite, _signature_name in tap.callsite_counter}
    assert "unattributed" not in attributed_callsites, f"untracked statement callsite: {sorted(tap.callsite_counter)}"

    device_lock = tap.callsite_counter[("app.devices.locking.lock_device_handle", "SELECT devices")]
    snapshot_claims = tap.callsite_counter[
        ("app.devices.services.decision_snapshot._load_claims_intents_and_reservation", "SELECT device_intents")
    ]
    snapshot_ladder = tap.callsite_counter[
        ("app.devices.services.decision_snapshot._load_current_ladder", "SELECT device_remediation_log")
    ]
    source_commits = commits.count
    # Any remediation-ladder read not attributed to the snapshot loader is a
    # standalone read Phase 3 removed; likewise any reservation-table select
    # (the snapshot folds the reservation into the claims/intents select).
    legacy_ladder_load = tap.counter["SELECT device_remediation_log"] - snapshot_ladder
    legacy_reservation_load = tap.counter["SELECT device_reservations"]

    budget = {
        "device_lock": device_lock,
        "snapshot_claims": snapshot_claims,
        "snapshot_ladder": snapshot_ladder,
        "source_commits": source_commits,
        "legacy_ladder_load": legacy_ladder_load,
        "legacy_reservation_load": legacy_reservation_load,
    }
    expected = {
        "device_lock": device_count,
        "snapshot_claims": device_count,
        "snapshot_ladder": device_count,
        # One per device, plus one for the fold's own inventory-read transaction
        # (Phase 10 gave that read an explicit ``db.begin()`` of its own).
        "source_commits": device_count + 1,
        "legacy_ladder_load": 0,
        "legacy_reservation_load": 0,
    }
    assert budget == expected, f"healthy-fold statement budget drift: {budget} != {expected}"
