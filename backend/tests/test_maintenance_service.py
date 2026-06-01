from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices import locking as device_locking
from app.devices.models import DeviceEvent, DeviceEventType, DeviceHold, DeviceOperationalState
from app.devices.services import maintenance as maintenance_service
from app.devices.services import state_write_guard
from app.devices.services.maintenance import MaintenanceService
from app.events.protocols import EventPublisher
from app.hosts.models import Host
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, settle_after_commit_tasks

pytestmark = pytest.mark.asyncio


async def test_enter_maintenance_emits_hold_changed_and_audit_row(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Entering maintenance must still emit device.hold_changed (SSE/webhooks) and record a
    maintenance_entered audit row — the reconciler is now the writer, so it must carry both."""
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-emits",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    publisher = AsyncMock(spec=EventPublisher)
    locked = await device_locking.lock_device(db_session, device.id)
    await MaintenanceService(settings=FakeSettingsReader({}), publisher=publisher).enter_maintenance(db_session, locked)
    await settle_after_commit_tasks()

    emitted = [call.args[0] for call in publisher.publish.call_args_list]
    assert "device.hold_changed" in emitted

    rows = (await db_session.execute(select(DeviceEvent).where(DeviceEvent.device_id == device.id))).scalars().all()
    assert any(r.event_type is DeviceEventType.maintenance_entered for r in rows)


async def test_enter_maintenance_rejects_reserved_device_by_default(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    from tests.helpers import create_reservation

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-target",
        hold=DeviceHold.reserved,
    )
    await db_session.commit()
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    with pytest.raises(ValueError) as exc:
        await MaintenanceService(settings=FakeSettingsReader({})).enter_maintenance(db_session, locked)

    assert "reserved" in str(exc.value).lower()


async def test_enter_maintenance_rejects_device_with_reservation_row_no_hold(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Reserved guard must use the reservation row, not device.hold.

    Device has hold=NULL but an active DeviceReservation row — this is the
    future state after hold is removed. The guard must still reject it.
    """
    from tests.helpers import create_reservation

    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reservation-row-target",
        hold=None,  # hold is NULL — future state
    )
    await db_session.commit()
    await create_reservation(db_session, device_id=device.id)
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    with pytest.raises(ValueError) as exc:
        await MaintenanceService(settings=FakeSettingsReader({})).enter_maintenance(db_session, locked)

    assert "reserved" in str(exc.value).lower()


async def test_enter_maintenance_allows_reserved_when_explicitly_overridden(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="forced-target",
        hold=DeviceHold.reserved,
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    result = await MaintenanceService(settings=FakeSettingsReader({})).enter_maintenance(
        db_session, locked, allow_reserved=True
    )

    # hold is now derived by the reconciler (Task 7+8); check the signal instead
    assert result.lifecycle_policy_state is not None
    assert result.lifecycle_policy_state.get("maintenance_reason") is not None


async def test_enter_maintenance_succeeds_for_available_device(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="happy-target",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    result = await MaintenanceService(settings=FakeSettingsReader({})).enter_maintenance(db_session, locked)

    # hold is now derived by the reconciler (Task 7+8); check the signal instead
    assert result.lifecycle_policy_state is not None
    assert result.lifecycle_policy_state.get("maintenance_reason") is not None


async def test_exit_maintenance_clears_maintenance_recovery_suppression(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Exiting maintenance must clear lifecycle_policy_state.recovery_suppressed_reason
    set by handle_health_failure while the device was held in maintenance.

    Without this, devices stay rendered as Unhealthy (recovery_state="suppressed")
    on the devices list even after the operator brought them back and the live
    checks pass — see the "Device is in maintenance mode" suppression set by
    lifecycle_policy.handle_health_failure.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-clears-suppression",
        hold=DeviceHold.maintenance,
        lifecycle_policy_state={
            "last_action": "recovery_suppressed",
            "last_action_at": "2026-05-09T21:14:19+00:00",
            "last_failure_reason": "Failed checks: ping, ecp",
            "last_failure_source": "device_checks",
            "recovery_suppressed_reason": "Device is in maintenance mode",
            "recovery_backoff_attempts": 0,
            "backoff_until": None,
            "stop_pending": False,
            "stop_pending_reason": None,
            "stop_pending_since": None,
            "maintenance_reason": "Operator entered maintenance",
        },
    )
    await db_session.commit()

    await MaintenanceService(settings=FakeSettingsReader({})).exit_maintenance(db_session, device)
    await db_session.refresh(device)

    # hold is now derived by the reconciler (Task 7+8); signal-cleared is what this test verifies
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("recovery_suppressed_reason") is None
    assert device.lifecycle_policy_state.get("backoff_until") is None
    assert device.lifecycle_policy_state.get("recovery_backoff_attempts") == 0
    assert device.lifecycle_policy_state.get("last_action") == "maintenance_exited"


async def test_exit_maintenance_preserves_non_maintenance_suppression(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    """Suppressions whose cause is independent of the maintenance hold
    (``"Node restart failed"``, ``"Recovery probe failed"``, an active backoff
    window, ...) describe a real condition that survives operator-driven
    maintenance exit and must NOT be silently wiped along with the
    maintenance-tautology reason.
    """
    backoff_until = "2027-01-01T00:00:00+00:00"
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-preserves-backoff-suppression",
        hold=DeviceHold.maintenance,
        lifecycle_policy_state={
            "last_action": "recovery_suppressed",
            "last_action_at": "2026-05-09T21:14:19+00:00",
            "last_failure_reason": "Max node health failures reached",
            "last_failure_source": "node_health",
            "recovery_suppressed_reason": "Node restart failed",
            "recovery_backoff_attempts": 3,
            "backoff_until": backoff_until,
            "stop_pending": False,
            "stop_pending_reason": None,
            "stop_pending_since": None,
            "maintenance_reason": "Operator entered maintenance",
        },
    )
    await db_session.commit()

    await MaintenanceService(settings=FakeSettingsReader({})).exit_maintenance(db_session, device)
    await db_session.refresh(device)

    # hold is now derived by the reconciler (Task 7+8); signal-cleared is what this test verifies
    assert device.lifecycle_policy_state is not None
    # Suppression unrelated to the maintenance hold must persist.
    assert device.lifecycle_policy_state.get("recovery_suppressed_reason") == "Node restart failed"
    assert device.lifecycle_policy_state.get("backoff_until") == backoff_until
    assert device.lifecycle_policy_state.get("recovery_backoff_attempts") == 3
    assert device.lifecycle_policy_state.get("last_action") == "recovery_suppressed"


async def test_enter_and_exit_maintenance_commit_false_branches(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-no-commit",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    from app.devices.services.lifecycle_policy_state import state as ps

    svc = MaintenanceService(settings=FakeSettingsReader({}))
    result = await svc.enter_maintenance(db_session, device, commit=False)
    # hold is derived by the reconciler (Task 7+8); check the signal instead
    assert ps(result).get("maintenance_reason") is not None

    result = await svc.exit_maintenance(db_session, device, commit=False)
    assert ps(result).get("maintenance_reason") is None

    with pytest.raises(ValueError, match="not in maintenance"):
        await svc.exit_maintenance(db_session, device, commit=False)


async def test_exit_maintenance_schedules_recovery_and_swallows_enqueue_failure(
    db_session: AsyncSession,
    db_host: Host,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="maintenance-schedules-recovery",
        hold=DeviceHold.maintenance,
        lifecycle_policy_state={"maintenance_reason": "Operator entered maintenance"},
    )
    await db_session.commit()

    svc = MaintenanceService(settings=FakeSettingsReader({}))
    schedule = AsyncMock()
    monkeypatch.setattr(maintenance_service, "_schedule_device_recovery", schedule)
    await svc.exit_maintenance(db_session, device)
    schedule.assert_awaited_once_with(db_session, device.id)

    with state_write_guard.bypass():
        device.hold = DeviceHold.maintenance
    from app.devices.services.lifecycle_policy_state import set_maintenance_reason

    set_maintenance_reason(device, "Operator entered maintenance")
    await db_session.commit()
    schedule.side_effect = RuntimeError("queue down")
    await svc.exit_maintenance(db_session, device)


async def test_enter_maintenance_stores_maintenance_reason(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reason-target",
        operational_state=DeviceOperationalState.available,
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    await MaintenanceService(settings=FakeSettingsReader({})).enter_maintenance(
        db_session, locked, maintenance_reason="Cooldown escalation"
    )

    await db_session.refresh(device)
    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("maintenance_reason") == "Cooldown escalation"


async def test_exit_maintenance_clears_maintenance_reason(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="clear-reason-target",
        hold=DeviceHold.maintenance,
        lifecycle_policy_state={
            "last_action": None,
            "last_action_at": None,
            "last_failure_reason": None,
            "last_failure_source": None,
            "recovery_suppressed_reason": "Device is in maintenance mode",
            "recovery_backoff_attempts": 0,
            "backoff_until": None,
            "stop_pending": False,
            "stop_pending_reason": None,
            "stop_pending_since": None,
            "maintenance_reason": "Cooldown escalation",
        },
    )
    await db_session.commit()

    await MaintenanceService(settings=FakeSettingsReader({})).exit_maintenance(db_session, device)
    await db_session.refresh(device)

    assert device.lifecycle_policy_state is not None
    assert device.lifecycle_policy_state.get("maintenance_reason") is None
