from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.devices.models import DeviceReservation
from app.runs.models import TestRun

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _reservation_entry_matches(entry: DeviceReservation, device_id: uuid.UUID | str) -> bool:
    return str(entry.device_id) == str(device_id)


def _reservation_entry_for_device(
    run: TestRun,
    device_id: uuid.UUID | str,
    *,
    active_only: bool = False,
) -> DeviceReservation | None:
    if not run.device_reservations:
        return None

    matching = [entry for entry in run.device_reservations if _reservation_entry_matches(entry, device_id)]
    if active_only:
        matching = [entry for entry in matching if entry.released_at is None]
    if not matching:
        return None
    return cast("DeviceReservation", matching[-1])


def _reservation_entry_is_excluded(entry: DeviceReservation) -> bool:
    if not entry.excluded:
        return False
    if entry.excluded_until is None:
        return True
    return entry.excluded_until > _now_utc()


async def get_run(db: AsyncSession, run_id: uuid.UUID) -> TestRun | None:
    stmt = (
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_run_for_update(db: AsyncSession, run_id: uuid.UUID) -> TestRun | None:
    stmt = (
        select(TestRun)
        .where(TestRun.id == run_id)
        .options(selectinload(TestRun.device_reservations).selectinload(DeviceReservation.device))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_device_reservation_with_entry(
    db: AsyncSession,
    device_id: uuid.UUID,
) -> tuple[TestRun | None, DeviceReservation | None]:
    stmt = (
        select(DeviceReservation)
        .where(DeviceReservation.device_id == device_id, DeviceReservation.released_at.is_(None))
        .options(selectinload(DeviceReservation.run).selectinload(TestRun.device_reservations))
        .order_by(DeviceReservation.created_at.desc())
    )
    result = await db.execute(stmt)
    reservation = result.scalars().first()
    if reservation is None:
        return None, None
    return cast("TestRun | None", reservation.run), reservation


def get_reservation_entry_for_device(run: TestRun, device_id: uuid.UUID | str) -> DeviceReservation | None:
    return _reservation_entry_for_device(run, device_id, active_only=True)


async def exclude_device_from_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    reason: str,
    commit: bool = True,
) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or entry is None:
        return None
    if _reservation_entry_is_excluded(entry) and entry.exclusion_reason == reason:
        return run

    entry.excluded = True
    entry.exclusion_reason = reason
    entry.excluded_at = _now_utc()
    entry.excluded_until = None
    if commit:
        await db.commit()
        run = await get_run(db, run.id)
    return run


async def restore_device_to_run(
    db: AsyncSession,
    device_id: uuid.UUID,
    *,
    commit: bool = True,
) -> TestRun | None:
    run, entry = await get_device_reservation_with_entry(db, device_id)
    if run is None or entry is None:
        return None
    if entry.excluded_until is not None and entry.excluded_until > _now_utc():
        return run
    if not _reservation_entry_is_excluded(entry):
        return run

    entry.excluded = False
    entry.exclusion_reason = None
    entry.excluded_at = None
    entry.excluded_until = None
    # Explicit restore is the sanctioned reset point for the cooldown
    # counter — the intent-TTL clear path deliberately leaves it sticky.
    entry.cooldown_count = 0
    # Same signal applies to the review-shelving flag: restoring a device
    # is an operator promise that it is ready for another attempt.
    from sqlalchemy.exc import NoResultFound  # noqa: PLC0415

    from app.devices import locking as device_locking  # noqa: PLC0415
    from app.devices.services.review import clear_review_required  # noqa: PLC0415

    try:
        device = await device_locking.lock_device(db, device_id, load_sessions=False)
    except (NoResultFound, AttributeError):
        # AttributeError reaches us only from in-process unit tests that
        # stub the session with a Fake that has no ``execute``. Production
        # callers always pass a real AsyncSession.
        device = None
    if device is not None:
        await clear_review_required(
            db,
            device,
            reason="Reservation restored to run",
            source="restore_device_to_run",
        )
    if commit:
        await db.commit()
        run = await get_run(db, run.id)
    return run


def reservation_entry_is_excluded(entry: DeviceReservation | None) -> bool:
    return bool(entry and _reservation_entry_is_excluded(entry))
