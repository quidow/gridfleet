import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import DeviceHold, DeviceOperationalState
from app.models.host import Host
from app.services.maintenance_service import enter_maintenance, exit_maintenance
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

    with pytest.raises(ValueError) as exc:
        await enter_maintenance(db_session, device)

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

    result = await enter_maintenance(db_session, device, allow_reserved=True, drain=True)

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

    result = await enter_maintenance(db_session, device, drain=True)

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
