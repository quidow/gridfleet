from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.devices import locking as device_locking
from app.devices.models import DeviceHold, DeviceOperationalState
from app.devices.services import maintenance as maintenance_service
from app.devices.services.maintenance import enter_maintenance, exit_maintenance
from app.hosts.models import Host
from tests.helpers import create_device

pytestmark = pytest.mark.asyncio


async def test_enter_maintenance_rejects_reserved_device_by_default(
    db_session: AsyncSession,
    db_host: Host,
) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="reserved-target",
        hold=DeviceHold.reserved,
    )
    await db_session.commit()

    locked = await device_locking.lock_device(db_session, device.id)
    with pytest.raises(ValueError) as exc:
        await enter_maintenance(db_session, locked)

    assert "reserved" in str(exc.value).lower()
    await db_session.refresh(device)
    assert device.hold == DeviceHold.reserved


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
    result = await enter_maintenance(db_session, locked, allow_reserved=True)

    assert result.hold == DeviceHold.maintenance


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
    result = await enter_maintenance(db_session, locked)

    assert result.hold == DeviceHold.maintenance


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
        },
    )
    await db_session.commit()

    await exit_maintenance(db_session, device)
    await db_session.refresh(device)

    assert device.hold is None
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
    (``"Auto-manage is disabled"``, ``"Node restart failed"``,
    ``"Recovery probe failed"``, an active backoff window, ...) describe a
    real condition that survives operator-driven maintenance exit and must
    NOT be silently wiped along with the maintenance-tautology reason.
    """
    backoff_until = "2027-01-01T00:00:00+00:00"
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="exit-preserves-auto-manage-suppression",
        hold=DeviceHold.maintenance,
        auto_manage=False,
        lifecycle_policy_state={
            "last_action": "recovery_suppressed",
            "last_action_at": "2026-05-09T21:14:19+00:00",
            "last_failure_reason": "Max node health failures reached",
            "last_failure_source": "node_health",
            "recovery_suppressed_reason": "Auto-manage is disabled",
            "recovery_backoff_attempts": 3,
            "backoff_until": backoff_until,
            "stop_pending": False,
            "stop_pending_reason": None,
            "stop_pending_since": None,
        },
    )
    await db_session.commit()

    await exit_maintenance(db_session, device)
    await db_session.refresh(device)

    assert device.hold is None
    assert device.lifecycle_policy_state is not None
    # Suppression unrelated to the maintenance hold must persist.
    assert device.lifecycle_policy_state.get("recovery_suppressed_reason") == "Auto-manage is disabled"
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

    result = await enter_maintenance(db_session, device, commit=False)
    assert result.hold == DeviceHold.maintenance

    result = await exit_maintenance(db_session, device, commit=False)
    assert result.hold is None

    with pytest.raises(ValueError, match="not in maintenance"):
        await exit_maintenance(db_session, device, commit=False)


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
    )
    await db_session.commit()

    schedule = AsyncMock()
    monkeypatch.setattr(maintenance_service, "schedule_device_recovery", schedule)
    await exit_maintenance(db_session, device)
    schedule.assert_awaited_once_with(db_session, device.id)

    device.hold = DeviceHold.maintenance
    await db_session.commit()
    schedule.side_effect = RuntimeError("queue down")
    await exit_maintenance(db_session, device)
