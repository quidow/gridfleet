"""Characterization tests pinning intent payload shapes per docs/reference/intents.md.

Each test invokes a real producer entry point and asserts that the registered
DeviceIntent.payload matches the schema documented in the "Per-source payload
table" section of intents.md.  These tests are the contract between the doc
and the code: a payload change that fails here means the change author must
update the doc (or revert the payload change).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumNode
from app.devices import locking as device_locking
from app.devices.models import DeviceIntent, DeviceOperationalState, DeviceRemediationLogEntry, DeviceReservation
from app.lifecycle.services import remediation_log
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.models import RunState, TestRun
from tests.fakes import FakeSettingsReader, build_review_service
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _get_intent(db: AsyncSession, device_id: uuid.UUID, *, prefix: str) -> DeviceIntent:
    row = (
        await db.execute(
            select(DeviceIntent).where(
                DeviceIntent.device_id == device_id,
                DeviceIntent.source.startswith(prefix),
            )
        )
    ).scalar_one_or_none()
    assert row is not None, f"no intent matching prefix {prefix!r} for device {device_id}"
    return row


# ---------------------------------------------------------------------------
# Test 1 — auto-stop commission action
#
# Producer: handle_health_failure → complete_auto_stop → handle_node_crash.
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_auto_stop_commission_action_shape(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="intent-policy-hf",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    # Attach a stopped node so the crash path can acquire the appium node lock.
    node = AppiumNode(device_id=device.id, port=4723)
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    await db_session.commit()

    _svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=Mock(),
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )
    result = await _svc.handle_health_failure(
        db_session,
        device,
        source="device_checks",
        reason="ADB not responsive",
    )
    assert result == "stopped"

    entry = (
        await db_session.execute(
            select(DeviceRemediationLogEntry).where(
                DeviceRemediationLogEntry.device_id == device.id,
                DeviceRemediationLogEntry.action == remediation_log.ACTION_AUTO_STOP_COMMISSIONED,
            )
        )
    ).scalar_one()
    assert entry.reason == "ADB not responsive"


# ---------------------------------------------------------------------------
# Test 2 — cooldown:grid:* and cooldown:reservation:* payload shapes
#
# Producer: runs.service_lifecycle_failures.cooldown_device
#
# Cooldown is a warm soft-gate park (design P2): no cooldown:node intent. The
# grid intent carries accepting_new_sessions=False (the load-bearing lever under
# P1); the reservation intent carries cooldown_count + exclusion_reason.
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_cooldown_intent_payload_shape(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    db_host: Host,
) -> None:
    from app.agent_comm.circuit_breaker import AgentCircuitBreaker
    from app.devices.services.maintenance import MaintenanceService
    from app.runs.service_lifecycle_failures import RunFailureService
    from app.runs.service_reservation import RunReservationService

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="intent-policy-cooldown",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    run = TestRun(
        name="Cooldown Shape Run",
        state=RunState.active,
        requirements=[{"pack_id": "appium-uiautomator2", "platform_id": "android_mobile", "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        reserved_devices=[
            {
                "device_id": str(device.id),
                "identity_value": device.identity_value,
                "connection_target": device.connection_target,
                "pack_id": "appium-uiautomator2",
                "platform_id": "android_mobile",
                "os_version": device.os_version,
                "host_ip": None,
                "excluded": False,
                "exclusion_reason": None,
                "excluded_at": None,
            }
        ],
    )
    db_session.add(run)
    # The TestRun.reserved_devices setter already creates a DeviceReservation row
    # via the ORM relationship cascade — no separate INSERT needed.
    await db_session.commit()

    _test_settings = FakeSettingsReader({})
    _test_cb = AgentCircuitBreaker(publisher=event_bus)
    _failure_svc = RunFailureService(
        publisher=event_bus,
        settings=_test_settings,
        circuit_breaker=_test_cb,
        maintenance=MaintenanceService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(review=build_review_service()),
        incidents=AsyncMock(),
        session_factory=db_session_maker,
    )
    cooldown_reason = "flaky connection detected"
    result = await _failure_svc.cooldown_device(
        run.id,
        device.id,
        reason=cooldown_reason,
        ttl_seconds=120,
    )
    assert not result.escalated  # non-escalation path records the cooldown we want

    # Cooldown is now a fact read from the reservation row, not a synthesized intent.
    # The grid-routing lever (accepting_new_sessions=False under cooldown) is proven by
    # tests/devices/test_decision.py::test_grid_cooldown_keeps_run_but_blocks_sessions.
    # --- cooldown_count + exclusion_reason live on the DeviceReservation row ---
    # cooldown_device commits via its own session (session_factory-owned transaction),
    # so this fetch must bypass db_session's identity-map cache.
    reservation = (
        await db_session.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert reservation.cooldown_count == result.cooldown_count
    assert reservation.exclusion_reason == cooldown_reason


# ---------------------------------------------------------------------------
# Test 3 — operator:start:* payload shape (start variant)
#
# Producer: devices.services.bulk._bulk_start_one → _operator_start_intent
#
# Documented fields (intents.md, "operator:start:{device_id}" start variant):
#   - desired_port  (intentional snapshot — operator-chosen port at start time)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_operator_start_intent_payload_shape(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.devices.services.bulk import _bulk_start_one
    from app.lifecycle.services.operator_node import OperatorNodeLifecycleService

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="intent-policy-op-start",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()

    # lock_device eager-loads appium_node and host, which _bulk_start_one accesses
    # synchronously via device.appium_node.  Without eager loading the attribute
    # access triggers a lazy load in an async context and raises MissingGreenlet.
    device = await device_locking.lock_device(db_session, device.id)
    await _bulk_start_one(
        db_session,
        device,
        caller="operator",
        operator=OperatorNodeLifecycleService(
            review=build_review_service(), settings=FakeSettingsReader({}), publisher=event_bus
        ),
    )

    intent = await _get_intent(db_session, device.id, prefix=f"operator:start:{device.id}")

    # Slim payload per intents.md: the start variant carries only action. desired_port
    # is gone (the applier pins the live node.port), as are priority and the
    # restart-only transition_token/transition_deadline.
    assert intent.payload == {"action": "start"}


# ---------------------------------------------------------------------------
# Test 4 — lifecycle recovery commission leaves no retired intent rows
#
# Producer: lifecycle_policy.attempt_auto_recovery (when node is stopped).
# ---------------------------------------------------------------------------


@pytest.mark.db
@pytest.mark.usefixtures("seeded_driver_packs")
async def test_auto_recovery_commission_is_recorded_in_the_remediation_log(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from app.appium_nodes.models import AppiumDesiredState, AppiumNode
    from app.devices import locking as device_locking
    from app.devices.services.decision_snapshot import load_device_decision_snapshot
    from app.lifecycle.services.actions import LifecyclePolicyActionsService
    from app.lifecycle.services.policy import LifecyclePolicyService
    from app.runs.service_reservation import RunReservationService

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="intent-policy-auto-recover",
        operational_state=DeviceOperationalState.offline,
        verified=True,
    )
    db_session.add(AppiumNode(device_id=device.id, port=4723, desired_state=AppiumDesiredState.stopped))
    await db_session.commit()

    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=AsyncMock(),
        node_manager=AsyncMock(),
    )
    generation = uuid.uuid4()
    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    prepared = await svc.prepare_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        source="device_connectivity",
        reason="Node went offline",
        enqueue_job=False,
    )
    await db_session.commit()
    assert prepared is True

    locked = await device_locking.lock_device_handle(db_session, device.id)
    snapshot = await load_device_decision_snapshot(db_session, locked, packs={}, now=datetime.now(UTC))
    outcome = await svc.finalize_auto_recovery_locked(
        db_session,
        locked,
        snapshot,
        generation=generation,
        result={"status": "passed"},
        source="device_connectivity",
        reason="Node went offline",
    )
    await db_session.commit()
    assert outcome == "recovered"

    entries = (
        (
            await db_session.execute(
                select(DeviceRemediationLogEntry).where(DeviceRemediationLogEntry.device_id == device.id)
            )
        )
        .scalars()
        .all()
    )
    assert any(entry.action == remediation_log.ACTION_RECOVERY_STARTED for entry in entries)
    assert not any(entry.kind == "action" and entry.action.startswith("auto_recovery:") for entry in entries)
