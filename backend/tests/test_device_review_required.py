"""Behaviour of the ``Device.review_required`` shelving flag.

Once a device has been promoted into this state, automated recovery loops
must skip it; only sanctioned operator actions clear it back.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.devices.models import DeviceHold, DeviceOperationalState
from app.devices.services.maintenance import enter_maintenance, exit_maintenance
from app.devices.services.review import clear_review_required, mark_review_required
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def test_mark_and_clear_review_required(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="review-toggle")

    set_result = await mark_review_required(
        db_session, device, reason="probe failed too many times", source="session_viability"
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert set_result is True
    assert device.review_required is True
    assert device.review_reason == "probe failed too many times"
    assert device.review_set_at is not None

    cleared = await clear_review_required(db_session, device, reason="operator action", source="operator")
    await db_session.commit()
    await db_session.refresh(device)
    assert cleared is True
    assert device.review_required is False
    assert device.review_reason is None
    assert device.review_set_at is None


async def test_mark_review_required_is_idempotent(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="review-idempotent")
    await mark_review_required(db_session, device, reason="initial", source="session_viability")
    await db_session.commit()

    second = await mark_review_required(db_session, device, reason="initial", source="session_viability")
    assert second is False


async def test_exit_maintenance_clears_review_required(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="review-cleared-on-exit",
        hold=DeviceHold.maintenance,
    )
    await mark_review_required(db_session, device, reason="stuck", source="session_viability")
    await db_session.commit()

    await exit_maintenance(db_session, device)
    await db_session.refresh(device)
    assert device.review_required is False
    assert device.review_reason is None


async def test_enter_maintenance_keeps_review_required(db_session: AsyncSession, db_host: Host) -> None:
    """Entering maintenance does NOT clear the flag — it is a separate signal.
    Only the exit transition (operator promise that the device is ready again)
    clears it.
    """
    device = await create_device(
        db_session,
        host_id=db_host.id,
        name="review-survives-enter",
        operational_state=DeviceOperationalState.available,
    )
    await mark_review_required(db_session, device, reason="stuck", source="session_viability")
    await db_session.commit()

    await enter_maintenance(db_session, device)
    await db_session.refresh(device)
    assert device.review_required is True


async def test_restore_device_to_run_clears_review_required(db_session: AsyncSession, db_host: Host) -> None:
    from app.runs import service as run_service

    device = await create_device(db_session, host_id=db_host.id, name="review-cleared-on-restore")
    await create_reserved_run(db_session, name="run-for-restore", devices=[device])

    # Seed a hard exclusion (post-escalation shape) and the review flag.
    from sqlalchemy import select

    from app.devices.models import DeviceReservation

    reservation = (
        await db_session.execute(select(DeviceReservation).where(DeviceReservation.device_id == device.id))
    ).scalar_one()
    from datetime import UTC, datetime, timedelta

    reservation.excluded = True
    reservation.exclusion_reason = "escalated"
    reservation.excluded_at = datetime.now(UTC) - timedelta(minutes=5)
    reservation.excluded_until = None
    reservation.cooldown_count = 3
    await mark_review_required(db_session, device, reason="stuck", source="session_viability")
    await db_session.commit()

    await run_service.restore_device_to_run(db_session, device.id)
    await db_session.refresh(device)
    assert device.review_required is False
    assert device.review_reason is None
