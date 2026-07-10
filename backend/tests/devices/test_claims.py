from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from app.devices.models import Device
from app.devices.models.intent import DeviceIntent
from app.devices.models.reservation import DeviceReservation
from app.devices.services.claims import (
    device_has_verification_lease,
    live_session_exists,
    reservation_active,
)
from app.devices.services.intent_types import CommandKind, verification_intent_source
from app.sessions.models import Session, SessionStatus
from tests.helpers import create_device, create_reservation

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host

pytestmark = pytest.mark.asyncio


def _lease(device_id: UUID, expires_at: datetime | None) -> DeviceIntent:
    return DeviceIntent(
        device_id=device_id,
        source=verification_intent_source(device_id),
        kind=CommandKind.verification_start,
        payload={"action": "start"},
        expires_at=expires_at,
    )


async def test_verification_lease_unexpired_counts(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="lease-live")
    db_session.add(_lease(device.id, datetime.now(UTC) + timedelta(minutes=5)))
    await db_session.flush()
    assert await device_has_verification_lease(db_session, device.id, now=datetime.now(UTC)) is True


async def test_verification_lease_expired_does_not_count(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="lease-expired")
    db_session.add(_lease(device.id, datetime.now(UTC) - timedelta(seconds=1)))
    await db_session.flush()
    assert await device_has_verification_lease(db_session, device.id, now=datetime.now(UTC)) is False


async def test_verification_lease_null_expiry_counts(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="lease-null-expiry")
    db_session.add(_lease(device.id, None))
    await db_session.flush()
    assert await device_has_verification_lease(db_session, device.id, now=datetime.now(UTC)) is True


async def test_live_session_exists_is_correlated_per_device(db_session: AsyncSession, db_host: Host) -> None:
    claimed = await create_device(db_session, host_id=db_host.id, name="claims-sess-claimed")
    free = await create_device(db_session, host_id=db_host.id, name="claims-sess-free")
    db_session.add(Session(session_id="alloc-claims-test", device_id=claimed.id, status=SessionStatus.pending))
    await db_session.flush()

    claimed_ids = set((await db_session.execute(select(Device.id).where(live_session_exists()))).scalars().all())
    assert claimed.id in claimed_ids
    assert free.id not in claimed_ids


async def test_reservation_active_row_clause(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="claims-res-active")
    reservation = await create_reservation(db_session, device_id=device.id)
    await db_session.flush()
    active = (
        (
            await db_session.execute(
                select(DeviceReservation).where(DeviceReservation.device_id == device.id, reservation_active())
            )
        )
        .scalars()
        .all()
    )
    assert len(active) == 1
    active[0].released_at = datetime.now(UTC)
    await db_session.flush()
    remaining = (
        (
            await db_session.execute(
                select(DeviceReservation).where(DeviceReservation.device_id == device.id, reservation_active())
            )
        )
        .scalars()
        .all()
    )
    assert remaining == []
    assert reservation.id == active[0].id
