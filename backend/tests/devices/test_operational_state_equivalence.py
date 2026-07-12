from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumDesiredState, AppiumNode
from app.devices.models import Device, DeviceOperationalState
from app.devices.services.fleet_capacity import _count_devices
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration, verification_intent_source
from app.devices.services.state import (
    emit_operational_state_transition,
    evaluate_operational_state,
    gather_device_state_facts,
    is_available_sql,
    is_busyish_sql,
    is_maintenance_sql,
    is_offline_sql,
    is_verifying_sql,
    operational_state_rank_sql,
    operational_state_sql,
)
from app.sessions.models import Session, SessionStatus
from app.sessions.probe_constants import PROBE_TEST_NAME
from tests.helpers import create_device_record, create_host
from tests.packs.factories import seed_test_packs

if TYPE_CHECKING:
    import asyncio

    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session as OrmSession
    from sqlalchemy.sql.expression import ColumnElement

    from app.events.catalog import EventSeverity


class _Pub:
    def queue_for_session(
        self,
        _db: AsyncSession | OrmSession,
        _event_type: str,
        _data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> None:
        pass

    async def publish(
        self,
        _event_type: str,
        _data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> None:
        pass

    def track_task(self, _task: asyncio.Task[None]) -> None:
        pass


class _RecordingPub(_Pub):
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def queue_for_session(
        self,
        _db: AsyncSession | OrmSession,
        _event_type: str,
        data: dict[str, Any],
        *,
        severity: EventSeverity | None = None,
    ) -> None:
        self.events.append({**data, "severity": severity})


async def _add_node(
    db_session: AsyncSession,
    device: Device,
    *,
    port: int,
    desired_state: AppiumDesiredState = AppiumDesiredState.running,
    health_running: bool | None = None,
    pid: int | None = None,
    active_connection_target: str | None = None,
    stop_pending: bool = False,
) -> None:
    db_session.add(
        AppiumNode(
            device_id=device.id,
            port=port,
            desired_state=desired_state,
            health_running=health_running,
            pid=pid,
            active_connection_target=active_connection_target,
            stop_pending=stop_pending,
        )
    )
    await db_session.flush()


@pytest.mark.db
async def test_operational_state_evaluator_and_sql_agree(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """The evaluator, SQL CASE, and predicates agree across the fact matrix."""
    await seed_test_packs(db_session)
    host = await create_host(client)
    now = datetime.now(UTC)
    scenarios: list[tuple[str, Device, DeviceOperationalState]] = []

    async def add_device(name: str, expected: DeviceOperationalState, **overrides: object) -> Device:
        device_overrides = dict(overrides)
        verified = bool(device_overrides.pop("verified", True))
        device = await create_device_record(
            db_session,
            host_id=host["id"],
            identity_value=f"equivalence-{len(scenarios):02d}",
            name=name,
            verified=verified,
            **cast("dict[str, Any]", device_overrides),
        )
        scenarios.append((name, device, expected))
        return device

    await add_device("baseline", DeviceOperationalState.available)

    running = await add_device("live-running", DeviceOperationalState.busy)
    db_session.add(Session(session_id="equivalence-running", device_id=running.id, status=SessionStatus.running))
    await db_session.flush()

    pending = await add_device("live-pending", DeviceOperationalState.busy)
    db_session.add(Session(session_id="equivalence-pending", device_id=pending.id, status=SessionStatus.pending))
    await db_session.flush()

    # WS-16.1 (D14): probe rows claim but do not mask — a live probe session
    # leaves the busy projection untouched, so probe cadence never flips the
    # device's ledger or emits operational-state edges.
    probing = await add_device("live-probe", DeviceOperationalState.available)
    db_session.add(
        Session(
            session_id="equivalence-probe",
            device_id=probing.id,
            test_name=PROBE_TEST_NAME,
            status=SessionStatus.running,
        )
    )
    await db_session.flush()

    leased = await add_device("verification-live", DeviceOperationalState.verifying)
    await IntentService(db_session).register_intents(
        device_id=leased.id,
        intents=[
            IntentRegistration(
                source=verification_intent_source(leased.id),
                kind=CommandKind.verification_start,
                payload={},
                expires_at=now + timedelta(minutes=5),
            )
        ],
    )
    await db_session.flush()

    expired = await add_device("verification-expired", DeviceOperationalState.available)
    await IntentService(db_session).register_intents(
        device_id=expired.id,
        intents=[
            IntentRegistration(
                source=verification_intent_source(expired.id),
                kind=CommandKind.verification_start,
                payload={},
                expires_at=now - timedelta(seconds=1),
            )
        ],
    )
    await db_session.flush()

    await add_device(
        "maintenance",
        DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "operator"},
    )
    maintained_running = await add_device(
        "maintenance-running",
        DeviceOperationalState.busy,
        lifecycle_policy_state={"maintenance_reason": "operator"},
    )
    db_session.add(
        Session(
            session_id="equivalence-maintenance-running",
            device_id=maintained_running.id,
            status=SessionStatus.running,
        )
    )
    await db_session.flush()

    await add_device("review-required", DeviceOperationalState.offline, review_required=True)
    await add_device("unverified", DeviceOperationalState.offline, verified=False)
    await add_device("device-checks-failed", DeviceOperationalState.offline, device_checks_healthy=False)
    await add_device("viability-failed", DeviceOperationalState.offline, session_viability_status="failed")

    health_down = await add_device("node-health-down", DeviceOperationalState.offline)
    await _add_node(db_session, health_down, port=4723, health_running=False)
    health_unknown = await add_device("node-health-unknown", DeviceOperationalState.offline)
    await _add_node(db_session, health_unknown, port=4724, health_running=None)
    stopped = await add_device("node-stopped", DeviceOperationalState.offline)
    await _add_node(db_session, stopped, port=4725, desired_state=AppiumDesiredState.stopped)
    pending_stop = await add_device("node-stop-pending", DeviceOperationalState.offline)
    await _add_node(db_session, pending_stop, port=4726, stop_pending=True)
    await add_device("no-node", DeviceOperationalState.available)
    await add_device(
        "maintenance-stopped",
        DeviceOperationalState.maintenance,
        lifecycle_policy_state={"maintenance_reason": "operator"},
    )
    maintenance_stopped = scenarios[-1][1]
    await _add_node(db_session, maintenance_stopped, port=4727, desired_state=AppiumDesiredState.stopped)

    predicate_builders: dict[str, ColumnElement[bool]] = {
        DeviceOperationalState.available.value: is_available_sql(now=now),
        DeviceOperationalState.busy.value: is_busyish_sql(),
        DeviceOperationalState.verifying.value: is_verifying_sql(now=now),
        DeviceOperationalState.offline.value: is_offline_sql(now=now),
        DeviceOperationalState.maintenance.value: is_maintenance_sql(now=now),
    }
    evaluated_by_id: dict[Any, DeviceOperationalState] = {}
    for scenario, device, expected in scenarios:
        facts = await gather_device_state_facts(db_session, device, now=now)
        evaluated = evaluate_operational_state(facts)
        assert evaluated is expected, f"{scenario}: evaluator returned {evaluated}, expected {expected}"
        sql_value = (
            await db_session.execute(select(operational_state_sql(now=now)).where(Device.id == device.id))
        ).scalar_one()
        assert evaluated.value == sql_value, f"{scenario}: evaluator {evaluated} != SQL {sql_value}"
        member = await db_session.execute(
            select(Device.id).where(Device.id == device.id, predicate_builders[sql_value])
        )
        assert member.first() is not None, f"{scenario}: per-state predicate disagrees with CASE {sql_value}"

        evaluated_by_id[device.id] = evaluated

    total, available, offline, maintenance = await _count_devices(db_session)
    assert total == len(scenarios)
    assert available == sum(state is DeviceOperationalState.available for state in evaluated_by_id.values())
    assert offline == sum(state is DeviceOperationalState.offline for state in evaluated_by_id.values())
    assert maintenance == sum(state is DeviceOperationalState.maintenance for state in evaluated_by_id.values())

    ordered_ids = (
        (await db_session.execute(select(Device.id).order_by(operational_state_rank_sql(now=now), Device.id)))
        .scalars()
        .all()
    )
    rank = {
        DeviceOperationalState.available: 0,
        DeviceOperationalState.busy: 1,
        DeviceOperationalState.offline: 2,
        DeviceOperationalState.verifying: 3,
        DeviceOperationalState.maintenance: 4,
    }
    expected_order = sorted(evaluated_by_id, key=lambda device_id: (rank[evaluated_by_id[device_id]], device_id))
    assert ordered_ids == expected_order

    # Accepted approximation: manifest-required fields are not SQL-expressible.
    # Allocation closes this gap with its full evaluator under the device lock.
    drift = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="equivalence-manifest-drift",
        name="manifest-drift",
        pack_id="appium-xcuitest",
        platform_id="ios",
        device_config={},
        verified=True,
    )
    drift_facts = await gather_device_state_facts(db_session, drift, now=now)
    assert evaluate_operational_state(drift_facts) is DeviceOperationalState.offline
    assert (
        await db_session.execute(select(is_available_sql(now=now)).where(Device.id == drift.id))
    ).scalar_one() is True


@pytest.mark.db
async def test_operational_state_edge_detector_is_exact_under_jitter(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    await seed_test_packs(db_session)
    host = await create_host(client)
    device = await create_device_record(
        db_session,
        host_id=host["id"],
        identity_value="equivalence-edge",
        name="edge detector",
        verified=True,
    )
    publisher = _RecordingPub()
    now = datetime.now(UTC)

    assert await emit_operational_state_transition(db_session, device, now=now, publisher=publisher) is True
    assert await emit_operational_state_transition(db_session, device, now=now, publisher=publisher) is False
    assert len(publisher.events) == 1
    assert publisher.events[0]["old_operational_state"] == DeviceOperationalState.offline.value
    assert publisher.events[0]["new_operational_state"] == DeviceOperationalState.available.value
    assert device.operational_state_last_emitted is DeviceOperationalState.available

    device.lifecycle_policy_state = {"maintenance_reason": "operator"}
    await db_session.flush()
    assert await emit_operational_state_transition(db_session, device, now=now, publisher=publisher) is True
    assert await emit_operational_state_transition(db_session, device, now=now, publisher=publisher) is False
    assert len(publisher.events) == 2
    assert publisher.events[-1]["old_operational_state"] == DeviceOperationalState.available.value
    assert publisher.events[-1]["new_operational_state"] == DeviceOperationalState.maintenance.value
    assert device.operational_state_last_emitted is DeviceOperationalState.maintenance
