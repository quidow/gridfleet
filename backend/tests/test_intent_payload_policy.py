"""Characterization tests pinning intent payload shapes per docs/reference/intents.md.

Each test invokes a real producer entry point and asserts that the registered
DeviceIntent.payload matches the schema documented in the "Per-source payload
table" section of intents.md.  These tests are the contract between the doc
and the code: a payload change that fails here means the change author must
update the doc (or revert the payload change).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest
from sqlalchemy import select

from app.appium_nodes.models import AppiumNode
from app.devices import locking as device_locking
from app.devices.models import DeviceIntent, DeviceOperationalState
from app.devices.services import state_write_guard
from app.lifecycle.services import policy as lifecycle_policy_module
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.runs.models import RunState, TestRun
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

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
# Test 1 — health_failure:node:* payload shape
#
# Producer: lifecycle_policy_actions._crash_intents() called via
#           handle_health_failure → complete_auto_stop → handle_node_crash →
#           register_intents_and_reconcile.
#
# Documented fields (intents.md, "health_failure:node:{device_id}"):
#   - stop_mode: "graceful"   (intentional snapshot)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_health_failure_intent_payload_shape(
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
    with state_write_guard.bypass():
        node = AppiumNode(device_id=device.id, port=4723, grid_url="http://hub:4444")
    db_session.add(node)
    await db_session.flush()
    device.appium_node = node
    await db_session.commit()

    _svc = LifecyclePolicyService(
        publisher=Mock(),
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=Mock(), reservation=RunReservationService(), incidents=LifecycleIncidentService()
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

    intent = await _get_intent(db_session, device.id, prefix=f"health_failure:node:{device.id}")
    payload = intent.payload

    # Documented fields per intents.md "health_failure:node:{device_id}" row:
    # stop_mode = "graceful" (intentional snapshot — policy fixed at detection time)
    assert payload.get("stop_mode") == "graceful", (
        f"health_failure:node intent stop_mode must be 'graceful'; got {payload!r}"
    )
    # Structural fields are always present
    assert payload.get("action") == "stop"
    assert "priority" in payload


# ---------------------------------------------------------------------------
# Test 2 — cooldown:node:* and cooldown:reservation:* payload shapes
#
# Producer: runs.service_lifecycle_failures.cooldown_device
#
# Documented fields (intents.md):
#   cooldown:node:{run_id}
#     - stop_mode: "defer"   (intentional snapshot)
#   cooldown:reservation:{run_id}
#     - cooldown_count        (refresh-on-event)
#     - exclusion_reason      (intentional snapshot, called "exclusion_reason" in model)
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_cooldown_intent_payload_shape(
    db_session: AsyncSession,
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
    _test_cb = AgentCircuitBreaker(publisher=event_bus, settings=_test_settings)
    _failure_svc = RunFailureService(
        publisher=event_bus,
        settings=_test_settings,
        circuit_breaker=_test_cb,
        maintenance=MaintenanceService(settings=FakeSettingsReader({}), publisher=event_bus),
        lifecycle_actions=AsyncMock(),
        reservation=RunReservationService(),
        health=AsyncMock(),
        incidents=AsyncMock(),
    )
    cooldown_reason = "flaky connection detected"
    _excluded_until, count, escalated, _ = await _failure_svc.cooldown_device(
        db_session,
        run.id,
        device.id,
        reason=cooldown_reason,
        ttl_seconds=120,
    )
    assert not escalated  # non-escalation path registers the intents we want

    # --- cooldown:node:{run_id} ---
    node_intent = await _get_intent(db_session, device.id, prefix=f"cooldown:node:{run.id}")
    node_payload = node_intent.payload

    # Documented: stop_mode = "defer" (intentional snapshot)
    assert node_payload.get("stop_mode") == "defer", (
        f"cooldown:node intent stop_mode must be 'defer'; got {node_payload!r}"
    )
    assert node_payload.get("action") == "stop"
    assert "priority" in node_payload

    # --- cooldown:reservation:{run_id} ---
    res_intent = await _get_intent(db_session, device.id, prefix=f"cooldown:reservation:{run.id}")
    res_payload = res_intent.payload

    # Documented: cooldown_count (refresh-on-event) and exclusion_reason (intentional snapshot)
    assert "cooldown_count" in res_payload, (
        f"cooldown:reservation intent must carry cooldown_count; got {res_payload!r}"
    )
    assert res_payload["cooldown_count"] == count
    assert "exclusion_reason" in res_payload, (
        f"cooldown:reservation intent must carry exclusion_reason; got {res_payload!r}"
    )
    assert res_payload["exclusion_reason"] == cooldown_reason


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
        operator=OperatorNodeLifecycleService(settings=FakeSettingsReader({}), publisher=event_bus),
    )

    intent = await _get_intent(db_session, device.id, prefix=f"operator:start:{device.id}")
    payload = intent.payload

    # Documented: desired_port is present (intentional snapshot)
    assert "desired_port" in payload, f"operator:start intent must carry desired_port; got {payload!r}"
    assert isinstance(payload["desired_port"], int)
    assert payload.get("action") == "start"
    assert "priority" in payload

    # Verify the start variant does NOT carry transition_token or transition_deadline
    # (those are restart-variant-only per the doc table).
    assert "transition_token" not in payload, "operator:start (start variant) must not carry transition_token"
    assert "transition_deadline" not in payload, "operator:start (start variant) must not carry transition_deadline"


# ---------------------------------------------------------------------------
# Test 4 — auto_recovery:node:* (lifecycle_policy path) omits desired_port
#
# Producer: lifecycle_policy.attempt_auto_recovery (when node is stopped)
#
# Documented (intents.md, "auto_recovery:node:{device_id}" lifecycle_policy path):
#   - (no extra fields) — desired_port was a Drop violation; removed in 864e6feb.
# ---------------------------------------------------------------------------


@pytest.mark.db
async def test_auto_recovery_intent_payload_omits_desired_port(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
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
    await db_session.commit()

    # Speed up the node-wait timeout and mock the viability probe so the test
    # does not need a real Appium process.  Mirrors the autouse fixture in
    # test_lifecycle_policy.py.
    probe_mock = AsyncMock(
        return_value={
            "status": "passed",
            "last_attempted_at": datetime.now(UTC).isoformat(),
            "last_succeeded_at": datetime.now(UTC).isoformat(),
            "error": None,
            "checked_by": "recovery",
        }
    )
    viability = AsyncMock()
    viability.run_session_viability_probe = probe_mock
    with patch.object(lifecycle_policy_module, "RECOVERY_NODE_START_WAIT_TIMEOUT_SEC", 0):
        recovered = await LifecyclePolicyService(
            publisher=event_bus,
            settings=FakeSettingsReader({}),
            actions=LifecyclePolicyActionsService(
                publisher=event_bus, reservation=RunReservationService(), incidents=LifecycleIncidentService()
            ),
            incidents=LifecycleIncidentService(),
            viability=viability,
            node_manager=AsyncMock(),
        ).attempt_auto_recovery(
            db_session,
            device,
            source="device_connectivity",
            reason="Node went offline",
        )
    assert recovered is True, "attempt_auto_recovery must return True for a fully-configured offline device"

    intent = await _get_intent(db_session, device.id, prefix=f"auto_recovery:node:{device.id}")
    payload = intent.payload

    # Key invariant: desired_port MUST NOT be in the payload (Drop violation removed in 864e6feb)
    assert "desired_port" not in payload, (
        f"auto_recovery:node (lifecycle_policy path) must NOT carry desired_port; got {payload!r}"
    )

    # Structural fields only per the doc table
    assert payload.get("action") == "start"
    assert "priority" in payload
