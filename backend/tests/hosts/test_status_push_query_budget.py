"""Statement and commit budgets for one consolidated status push.

Phase 8 split the push path into short, explicit transactions. This module is
the proof that the split did not multiply database work: it drives the real
``POST /agent/hosts/status`` endpoint with production-shaped wiring against
1-, 10-, and 50-device hosts and pins what the engine actually executes.

The pinned constants are MEASURED, not derived. ``FORMULA_MAX`` is the Phase 8
Global-Constraints ceiling (``24 + 9n``) and is asserted separately; a measured
count above it is an implementation defect, never a reason to raise the formula.
"""

from __future__ import annotations

import functools
from collections import Counter
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event

from app.appium_nodes.services.heartbeat import HeartbeatService
from app.appium_nodes.services.reconciler import ReconcilerService, converge_pushed_host
from app.core.metrics_recorders import HOST_PUSH_OBSERVATION_FAILURES
from app.core.timeutil import now_utc
from app.devices.services.property_refresh import PropertyRefreshService
from app.hosts.dependencies import get_host_services
from app.hosts.service import HostCrudService
from app.hosts.service_diagnostics import HostDiagnosticsService
from app.hosts.service_host_events import HostEventsService
from app.hosts.service_resource_telemetry import HostResourceTelemetryService
from app.hosts.service_status_push import HostStatusPushService, ObservationFold
from app.hosts.services_container import HostServices
from app.main import app
from app.packs.services.discovery import PackDiscoveryService
from tests.bench_instrumentation import CommitTap, QueryTap
from tests.fakes import FakeSettingsReader
from tests.fold_fixtures import HOMOGENEOUS_FLEET, seed_fleet
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host
    from tests.fold_fixtures import SeededDevice

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")

FLEET_SIZES = (1, 10, 50)

# MEASURED on this branch, not derived. Captured inventory per push (identical
# shape at all three sizes; n is the fleet size):
#   constant: 4 SELECT hosts, 3 SELECT control_plane_state_entries,
#             2 SELECT devices (desired rows + properties inventory),
#             3 SELECT device_remediation_log, 2 SELECT <sequence>,
#             2 SELECT host_resource_samples, 2 INSERT control_plane_state_entries,
#             1 INSERT host_resource_samples, 1 UPDATE hosts, 1 UPDATE appium_nodes
#   per device: 1 SELECT devices + 1 SELECT hosts (the properties fold's
#             per-device `get(Device, selectinload(Device.host))`) + 1 UPDATE devices
# => 21 + 3n statements and 5 + n commits. Lower these when a reduction lands;
# never raise one without attaching the inventory that explains the new statement.
STATUS_PUSH_MAX = {1: 24, 10: 51, 50: 171}
STATUS_PUSH_COMMITS = {1: 6, 10: 15, 50: 55}

# Phase 8 Global Constraints ceiling. Asserted separately from the measurement:
# a count above this is a Task 2/3/5 defect, not a reason to raise the formula.
FORMULA_MAX = {n: 24 + 9 * n for n in FLEET_SIZES}


def _build_push_service(session_factory: async_sessionmaker[AsyncSession]) -> HostStatusPushService:
    """Mirror app/composition.py's status-push wiring.

    Only the agent-dialing seams are doubled (``PackDiscoveryService``'s HTTP
    provider, the reconciler's pool/circuit breaker); every database path is the
    production one, which is what the budget measures.
    """
    settings = FakeSettingsReader({})
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
        # node_health and device_health fold off the request path on the
        # StatusFoldLoop, exactly as production wires them.
        observation_folds=(
            ObservationFold("device_properties", property_refresh.fold_host_device_properties),
            ObservationFold("host_telemetry", resource_telemetry.fold_host_telemetry),
        ),
        converge_host=functools.partial(converge_pushed_host, session_factory=session_factory, reconciler=reconciler),
        ingest_restart_events=heartbeat.ingest_restart_events,
    )


def _install_push_wiring(session_factory: async_sessionmaker[AsyncSession]) -> None:
    """Replace the conftest client's deliberately-bare status-push service.

    The default override wires no folds, convergence, or restart ingest, so a
    push through it would measure the endpoint's liveness phase alone.
    """
    settings = FakeSettingsReader({})
    services = HostServices(
        crud=HostCrudService(publisher=event_bus, settings=settings),
        resource_telemetry=HostResourceTelemetryService(settings=settings),
        diagnostics=HostDiagnosticsService(circuit_breaker=Mock()),
        host_events=HostEventsService(),
        status_push=_build_push_service(session_factory),
        settings=settings,
        session_factory=session_factory,
    )
    app.dependency_overrides[get_host_services] = lambda: services


def _push_payload(devices: list[SeededDevice]) -> dict[str, Any]:
    """One consolidated push for a whole host.

    ``appium_processes`` reports every seeded node exactly as the database
    already records it, so convergence confirms rather than writes — the
    steady state a healthy host pushes every 10 s. ``device_properties``
    carries drifted OS versions so the per-device settlement boundary Phase 8
    isolated (one fresh session and one commit per device) is actually
    exercised rather than short-circuited.
    """
    reported_at = now_utc().isoformat()
    return {
        "appium_processes": {
            "running_nodes": [
                {
                    "port": device.port,
                    "pid": device.pid,
                    "connection_target": device.identity,
                    "platform_id": device.spec.platform_id,
                }
                for device in devices
            ],
            "recent_restart_events": [],
            "start_failures": [],
        },
        "host_telemetry": {
            "recorded_at": reported_at,
            "cpu_percent": 42.0,
            "memory_used_mb": 8000,
            "memory_total_mb": 16000,
            "disk_used_gb": 100.0,
            "disk_total_gb": 500.0,
            "disk_percent": 20.0,
        },
        "node_health": {
            "reported_at": reported_at,
            "nodes": [
                {
                    "port": device.port,
                    "pid": device.pid,
                    "connection_target": device.identity,
                    "running": True,
                    "observed_at": reported_at,
                }
                for device in devices
            ],
        },
        "device_health": {
            "reported_at": reported_at,
            "devices": [
                {
                    "device_id": str(device.device_id),
                    "probe_status": "observed",
                    "presence": "present",
                    "health": {"healthy": True, "checks": []},
                    "lifecycle_state": {"status": "unsupported", "value": None},
                }
                for device in devices
            ],
        },
        "device_properties": {
            "reported_at": reported_at,
            "devices": {
                device.identity: {
                    "identity_value": device.identity,
                    "detected_properties": {"os_version": device.spec.drift_os_version},
                }
                for device in devices
            },
        },
    }


def _observation_failure_total() -> float:
    """Sum every child counter of HOST_PUSH_OBSERVATION_FAILURES.

    ``process_observations`` swallows per-stage exceptions and bumps this. A
    wiring gap would silently skip a stage and undercount the budget, so the
    measurement is only trustworthy while this total does not move.
    """
    return sum(
        sample.value
        for metric in HOST_PUSH_OBSERVATION_FAILURES.collect()
        for sample in metric.samples
        if sample.name.endswith("_total")
    )


async def _measure_push(
    client: AsyncClient,
    engine: Any,  # noqa: ANN401 - SQLAlchemy sync Engine, only ever handed to event.listen
    host: Host,
    devices: list[SeededDevice],
) -> tuple[QueryTap, CommitTap]:
    """Drive one real push and return the statements/commits it executed.

    The engine-scoped listeners see every connection in the pool, so they are
    attached only AFTER the fixture and seed statements have run and removed
    immediately after the response: no seeding, teardown, or unrelated
    connection traffic can inflate the count.
    """
    payload = {"host_id": str(host.id), **_push_payload(devices)}
    tap = QueryTap()
    commits = CommitTap()
    event.listen(engine, "before_cursor_execute", tap)
    event.listen(engine, "commit", commits)
    try:
        response = await client.post("/agent/hosts/status", json=payload)
    finally:
        event.remove(engine, "before_cursor_execute", tap)
        event.remove(engine, "commit", commits)
    assert response.status_code == 204
    return tap, commits


def _by_verb(tap: QueryTap) -> Counter[str]:
    """Statements grouped by normalized leading SQL keyword."""
    grouped: Counter[str] = Counter()
    for signature, count in tap.counter.items():
        grouped[signature.split(maxsplit=1)[0]] += count
    return grouped


async def test_status_push_statement_and_commit_budget(
    client: AsyncClient,
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
) -> None:
    _install_push_wiring(db_session_maker)
    assert db_session.bind is not None
    engine = db_session.bind.sync_engine
    failures_before = _observation_failure_total()

    counts: dict[int, int] = {}
    commits: dict[int, int] = {}
    verbs: dict[int, Counter[str]] = {}
    for generation, size in enumerate(FLEET_SIZES):
        host, devices = await seed_fleet(db_session, HOMOGENEOUS_FLEET, size, generation=generation)
        tap, commit_tap = await _measure_push(client, engine, host, devices)
        counts[size] = tap.total
        commits[size] = commit_tap.count
        verbs[size] = _by_verb(tap)
        print(f"\nstatus push n={size}: statements={tap.total} commits={commit_tap.count} verbs={dict(verbs[size])}")
        for signature, count in tap.counter.most_common():
            print(f"    {count:5d}  {signature}")

    # A wiring gap would make process_observations skip a stage silently, and a
    # skipped stage measures a budget nobody runs.
    assert _observation_failure_total() == failures_before, "a push stage failed (check the wiring)"
    assert counts[1] > 0, "the tap counted no statements at all"
    assert counts[50] > counts[1], "the push must do more work for 50 devices than for 1"
    for size in FLEET_SIZES:
        # The per-device settlement boundary really ran: one property write each.
        assert verbs[size]["UPDATE"] >= size, f"the device_properties fold wrote nothing at {size} devices"

    for size in FLEET_SIZES:
        assert counts[size] <= STATUS_PUSH_MAX[size], (
            f"status push at {size} devices issued {counts[size]} statements, above the pinned "
            f"{STATUS_PUSH_MAX[size]}: attach a captured statement inventory before raising this"
        )
        assert counts[size] <= FORMULA_MAX[size], (
            f"status push at {size} devices issued {counts[size]} statements, above the Phase 8 "
            f"ceiling {FORMULA_MAX[size]} — fix the implementation, do not raise the formula"
        )
    assert counts[10] - counts[1] <= 9 * 9
    assert counts[50] - counts[10] <= 40 * 9

    # A new commit is a transaction-boundary regression even when the statement
    # total stays under the formula, so these are exact.
    assert commits == STATUS_PUSH_COMMITS

    # No statement category may grow faster than the nine-per-device term.
    for verb in set(verbs[1]) | set(verbs[10]) | set(verbs[50]):
        assert verbs[10][verb] - verbs[1][verb] <= 9 * 9, f"{verb} grew faster than 9/device between 1 and 10 devices"
        assert verbs[50][verb] - verbs[10][verb] <= 40 * 9, (
            f"{verb} grew faster than 9/device between 10 and 50 devices"
        )
