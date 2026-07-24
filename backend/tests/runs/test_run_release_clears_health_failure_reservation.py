from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.core.timeutil import now_utc
from app.devices.models import DeviceReservation
from app.runs.service_lifecycle import RunLifecycleService
from app.runs.service_lifecycle_release import RunReleaseService
from tests.fakes import FakeSettingsReader
from tests.helpers import create_device, create_reserved_run
from tests.helpers import test_event_bus as event_bus

_settings = FakeSettingsReader({})
_release_svc = RunReleaseService(
    publisher=event_bus,
    settings=_settings,
    deferred_stop=AsyncMock(),
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.hosts.models import Host


def _make_lifecycle_svc(session_factory: async_sessionmaker[AsyncSession]) -> RunLifecycleService:
    return RunLifecycleService(
        publisher=event_bus, settings=_settings, release=_release_svc, session_factory=session_factory
    )


async def _fetch_reservation(db_session: AsyncSession, *, device_id: object) -> DeviceReservation:
    # The lifecycle command now commits via its own session (session_factory-owned
    # transaction), so this fetch must bypass db_session's identity-map cache to see it.
    return (
        await db_session.execute(
            select(DeviceReservation)
            .where(DeviceReservation.device_id == device_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _seed_health_failure_exclusion(
    db_session: AsyncSession,
    *,
    device_id: object,
    reason: str = "stale exclusion text",
) -> None:
    reservation = (
        await db_session.execute(
            select(DeviceReservation).where(
                DeviceReservation.device_id == device_id,
                DeviceReservation.released_at.is_(None),
            )
        )
    ).scalar_one()
    reservation.excluded = True
    reservation.exclusion_reason = reason
    reservation.excluded_at = now_utc()
    reservation.excluded_until = None
    await db_session.commit()


async def test_cancel_run_clears_health_failure_exclusion(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="cancel-release")
    run = await create_reserved_run(db_session, name="cancel-release-run", devices=[device])
    await _seed_health_failure_exclusion(db_session, device_id=device.id)
    seeded = await _fetch_reservation(db_session, device_id=device.id)
    assert seeded.excluded is True

    await _make_lifecycle_svc(db_session_maker).cancel_run(run.id)

    entry = await _fetch_reservation(db_session, device_id=device.id)
    # Run end releases the reservation; a released row no longer gates the device
    # (terminal-run reservations are ignored by reservation_gating_run_id), so the
    # stale exclusion is no longer live.
    assert entry.released_at is not None
    assert entry.excluded is False  # released rows are cleared of exclusion (invariant)


async def test_complete_run_clears_health_failure_exclusion(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="complete-release")
    run = await create_reserved_run(db_session, name="complete-release-run", devices=[device])
    await _seed_health_failure_exclusion(db_session, device_id=device.id)

    await _make_lifecycle_svc(db_session_maker).complete_run(run.id)

    entry = await _fetch_reservation(db_session, device_id=device.id)
    # Run end releases the reservation; a released row no longer gates the device
    # (terminal-run reservations are ignored by reservation_gating_run_id), so the
    # stale exclusion is no longer live.
    assert entry.released_at is not None
    assert entry.excluded is False  # released rows are cleared of exclusion (invariant)


async def test_expire_run_clears_health_failure_exclusion(
    db_session: AsyncSession, db_session_maker: async_sessionmaker[AsyncSession], db_host: Host
) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expire-release")
    run = await create_reserved_run(db_session, name="expire-release-run", devices=[device])
    await _seed_health_failure_exclusion(db_session, device_id=device.id)

    await _make_lifecycle_svc(db_session_maker).expire_run(run.id, "Heartbeat timeout")

    entry = await _fetch_reservation(db_session, device_id=device.id)
    # Run end releases the reservation; a released row no longer gates the device
    # (terminal-run reservations are ignored by reservation_gating_run_id), so the
    # stale exclusion is no longer live.
    assert entry.released_at is not None
    assert entry.excluded is False  # released rows are cleared of exclusion (invariant)
