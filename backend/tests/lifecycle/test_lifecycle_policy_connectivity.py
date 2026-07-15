"""D1: connectivity loss must NOT exclude device from its active run."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

from app.devices import locking as device_locking
from app.devices.models import ConnectionType, Device, DeviceOperationalState, DeviceType, ExclusionKind
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import CommandKind, IntentRegistration
from app.lifecycle.services import remediation_log
from app.lifecycle.services.actions import LifecyclePolicyActionsService
from app.lifecycle.services.incidents import LifecycleIncidentService
from app.lifecycle.services.policy import LifecyclePolicyService
from app.runs import service_reservation as run_reservation_service
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import RunReservationService
from tests.fakes import build_review_service
from tests.fakes.settings import FakeSettingsReader
from tests.helpers import create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.usefixtures("seeded_driver_packs")


def _build_lifecycle_policy_service() -> LifecyclePolicyService:
    from tests.helpers import test_event_bus as event_bus

    return LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=FakeSettingsReader({}),
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )


async def _make_available_device(db_session: AsyncSession, db_host: Host, *, identity: str) -> Device:
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value=identity,
        connection_target=identity,
        name=f"Self-heal device {identity}",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()
    return device


async def _register_operator_deny(db_session: AsyncSession, device: Device) -> None:
    # A real operator stop registers both the node-process stop and the recovery
    # deny; operator_stop_active (the N13 stickiness gate) keys on the node stop.
    await IntentService(db_session).register_intents(
        device_id=device.id,
        intents=[
            IntentRegistration(
                source=f"operator:stop:node:{device.id}",
                kind=CommandKind.operator_stop,
                payload={"action": "stop"},
            ),
            IntentRegistration(
                source=f"operator:stop:recovery:{device.id}",
                kind=CommandKind.operator_recovery_deny,
                payload={"allowed": False, "reason": "Operator stopped the node"},
            ),
        ],
    )


async def _seed_escalation_residue(db_session: AsyncSession, device: Device) -> None:
    settings = FakeSettingsReader(
        {
            "general.lifecycle_recovery_backoff_base_sec": 60,
            "general.lifecycle_recovery_backoff_max_sec": 900,
        }
    )
    for _ in range(2):
        await remediation_log.append_attempt(
            db_session,
            device.id,
            source="session_viability",
            reason="Recovery viability probe failed",
            settings=settings,
        )


async def test_self_heal_clears_residue_without_operator_hold(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """A healthy device with stale escalation residue and no deny intent gets
    the residue cleared in one cycle; a second cycle is a no-op (no churn)."""
    device = await _make_available_device(db_session, db_host, identity="self-heal-clear-1")
    locked = await device_locking.lock_device(db_session, device.id)
    await _seed_escalation_residue(db_session, locked)
    await db_session.commit()

    svc = _build_lifecycle_policy_service()

    locked = await device_locking.lock_device(db_session, device.id)
    cleared = await svc.clear_escalation_residue_on_self_heal(db_session, locked, reason="self-heal")
    assert cleared is True
    await db_session.refresh(locked)
    state_after = await remediation_log.load_ladder(db_session, locked.id)
    assert state_after.last_action == "self_healed"
    assert state_after.last_failure_reason is None
    assert state_after.backoff_until is None
    assert state_after.attempts == 0

    # Second cycle: nothing left to clear -> no-op, no action churn.
    locked = await device_locking.lock_device(db_session, device.id)
    cleared_again = await svc.clear_escalation_residue_on_self_heal(db_session, locked, reason="self-heal")
    assert cleared_again is False
    await db_session.refresh(locked)
    assert (await remediation_log.load_ladder(db_session, locked.id)).last_action == "self_healed"


async def test_self_heal_clear_does_not_commit_callers_transaction(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """The connectivity fold owns one transaction per settled device.

    ``clear_escalation_residue_on_self_heal`` must not commit that transaction:
    a later exception for the same device must still roll back the partial state.
    """
    device = await _make_available_device(db_session, db_host, identity="self-heal-no-commit")
    device_id = device.id
    locked = await device_locking.lock_device(db_session, device_id)
    await _seed_escalation_residue(db_session, locked)
    await db_session.commit()

    svc = _build_lifecycle_policy_service()
    locked = await device_locking.lock_device(db_session, device_id)
    cleared = await svc.clear_escalation_residue_on_self_heal(db_session, locked, reason="self-heal")
    assert cleared is True

    # Simulate a later failure in the same connectivity cycle.
    await db_session.rollback()

    locked = await device_locking.lock_device(db_session, device_id)
    state_after = await remediation_log.load_ladder(db_session, locked.id)
    assert state_after.last_failure_reason == "Recovery viability probe failed"
    assert state_after.attempts == 2


async def test_self_heal_does_not_clear_under_operator_stop_deny(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """An active operator-stop deny intent makes the hold a legitimate
    operator-owned condition — it must stay sticky (N13)."""
    device = await _make_available_device(db_session, db_host, identity="self-heal-deny-1")
    await _register_operator_deny(db_session, device)
    locked = await device_locking.lock_device(db_session, device.id)
    await _seed_escalation_residue(db_session, locked)
    await db_session.commit()

    svc = _build_lifecycle_policy_service()

    locked = await device_locking.lock_device(db_session, device.id)
    cleared = await svc.clear_escalation_residue_on_self_heal(db_session, locked, reason="self-heal")
    assert cleared is False
    await db_session.refresh(locked)
    state_after = await remediation_log.load_ladder(db_session, locked.id)
    assert state_after.last_failure_reason == "Recovery viability probe failed"
    assert state_after.last_action == "recovery_failed"


async def test_self_heal_clears_in_flight_residue_immediately(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """WS-15.1 successor semantics allow an immediate append-only reset."""
    device = await _make_available_device(db_session, db_host, identity="self-heal-fresh-1")
    locked = await device_locking.lock_device(db_session, device.id)
    await _seed_escalation_residue(db_session, locked)
    await db_session.commit()

    svc = _build_lifecycle_policy_service()

    locked = await device_locking.lock_device(db_session, device.id)
    cleared = await svc.clear_escalation_residue_on_self_heal(db_session, locked, reason="self-heal")
    assert cleared is True
    await db_session.refresh(locked)
    state_after = await remediation_log.load_ladder(db_session, locked.id)
    assert state_after.last_failure_reason is None
    assert state_after.last_action == "self_healed"


async def test_reconcile_self_heal_locked_clears_residue_and_restores_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await _make_available_device(db_session, db_host, identity="self-heal-locked")
    device_id = device.id
    await _seed_escalation_residue(db_session, device)
    await create_reserved_run(
        db_session,
        name="self-heal-locked-run",
        devices=[device],
        excluded_device_ids={str(device_id)},
        exclusion_reason="Failed checks: ping",
    )
    _run, entry = await run_reservation_service.get_device_reservation_with_entry(db_session, device_id)
    assert entry is not None
    entry.exclusion_kind = ExclusionKind.exclusion
    await db_session.commit()

    locked = await device_locking.lock_device_handle(db_session, device_id)
    result = await _build_lifecycle_policy_service().reconcile_self_heal_locked(
        db_session,
        locked,
        operational_state=DeviceOperationalState.available,
        residue_reason="Device self-healed after healthy reconnect",
        run_reason="Device healthy after self-heal",
    )

    assert result == (True, True)
    ladder = await remediation_log.load_ladder(db_session, device_id)
    assert ladder.last_action == "self_healed"
    _run, entry = await run_reservation_service.get_device_reservation_with_entry(db_session, device_id)
    assert entry is not None
    assert entry.excluded is False

    await db_session.rollback()

    ladder = await remediation_log.load_ladder(db_session, device_id)
    assert ladder.last_failure_reason == "Recovery viability probe failed"
    _run, entry = await run_reservation_service.get_device_reservation_with_entry(db_session, device_id)
    assert entry is not None
    assert entry.excluded is True


async def test_connectivity_loss_keeps_device_in_run(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """note_connectivity_loss must NOT mark the reservation entry excluded."""
    device = Device(
        pack_id="appium-uiautomator2",
        platform_id="android_mobile",
        identity_scheme="android_serial",
        identity_scope="host",
        identity_value="conn-loss-d1-1",
        connection_target="conn-loss-d1-1",
        name="Connectivity Loss D1 Device",
        os_version="14",
        host_id=db_host.id,
        operational_state=DeviceOperationalState.available,
        verified_at=datetime.now(UTC),
        device_type=DeviceType.real_device,
        connection_type=ConnectionType.usb,
    )
    db_session.add(device)
    await db_session.flush()

    run = TestRun(
        name="Active Run D1",
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
    await db_session.commit()

    from tests.helpers import test_event_bus as event_bus

    locked = await device_locking.lock_device(db_session, device.id)
    svc = LifecyclePolicyService(
        review=build_review_service(),
        publisher=event_bus,
        settings=None,  # type: ignore[arg-type]
        actions=LifecyclePolicyActionsService(
            publisher=event_bus,
            reservation=RunReservationService(review=build_review_service()),
            incidents=LifecycleIncidentService(),
        ),
        incidents=LifecycleIncidentService(),
        viability=Mock(),
        node_manager=AsyncMock(),
    )
    await svc.note_connectivity_loss(db_session, locked, reason="Heartbeat timeout")
    await db_session.commit()

    # Reservation entry must still be active (not excluded).
    fresh_run, entry = await run_reservation_service.get_device_reservation_with_entry(db_session, device.id)
    assert fresh_run is not None, "Run reservation must still exist"
    assert fresh_run.id == run.id
    assert entry is not None, "Reservation entry must still exist"
    assert run_reservation_service.reservation_entry_is_excluded(entry) is False, (
        "note_connectivity_loss must NOT exclude the device from its active run"
    )
