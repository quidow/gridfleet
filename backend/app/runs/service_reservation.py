from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import selectinload

from app.core.timeutil import now_utc
from app.devices import locking as device_locking
from app.devices.models import Device, DeviceReservation, ExclusionKind
from app.devices.services.claims import reservation_active
from app.devices.services.intent import IntentService
from app.runs.models import TERMINAL_STATES, TestRun

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.elements import ColumnElement

    from app.devices.locking import LockedDevice
    from app.devices.protocols import ReviewProtocol
    from app.events.protocols import EventPublisher


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
    return entry.excluded_until > now_utc()


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
        .where(DeviceReservation.device_id == device_id, reservation_active())
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


async def _lock_active_reservation_entry(
    db: AsyncSession,
    entry: DeviceReservation,
) -> DeviceReservation | None:
    """Re-fetch ``entry`` under ``SELECT ... FOR UPDATE WHERE released_at IS NULL``.

    The unlocked ``get_device_reservation_with_entry`` snapshot can be
    invalidated by a concurrent ``_release_devices`` commit that lands
    ``released_at = NOW``. Calling sites that mutate ``excluded`` /
    cooldown fields MUST proceed only against the locked, still-active
    row — otherwise the ORM-buffered write flushes onto a released
    reservation, leaving the row in a contradictory state.

    Returns ``None`` when the reservation was released between the
    unlocked read and the locked re-fetch.
    """
    return await _lock_active_reservation_entry_by_id(db, entry.id)


async def _lock_active_reservation_entry_by_id(
    db: AsyncSession,
    reservation_id: uuid.UUID,
) -> DeviceReservation | None:
    stmt = (
        select(DeviceReservation)
        .where(
            DeviceReservation.id == reservation_id,
            reservation_active(),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def get_device_reservation_map(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[uuid.UUID, TestRun]:
    if not device_ids:
        return {}
    stmt = (
        select(DeviceReservation)
        .where(DeviceReservation.device_id.in_(device_ids), reservation_active())
        .options(selectinload(DeviceReservation.run).selectinload(TestRun.device_reservations))
    )
    result = await db.execute(stmt)
    reservation_map: dict[uuid.UUID, TestRun] = {}
    for reservation in result.scalars().all():
        reservation_map[reservation.device_id] = reservation.run
    return reservation_map


def get_reservation_context_for_device(
    run: TestRun | None,
    device_id: uuid.UUID | str,
) -> tuple[TestRun | None, DeviceReservation | None]:
    if run is None:
        return None, None
    return run, get_reservation_entry_for_device(run, device_id)


async def get_device_reservation(db: AsyncSession, device_id: uuid.UUID) -> TestRun | None:
    reservation_map = await get_device_reservation_map(db, [device_id])
    return reservation_map.get(device_id)


class RunReservationService:
    def __init__(self, *, review: ReviewProtocol) -> None:
        self._review = review

    async def exclude_device_from_run(
        self,
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
        locked_entry = await _lock_active_reservation_entry(db, entry)
        if locked_entry is None:
            if commit:
                await db.commit()
            return run
        locked_entry.excluded = True
        locked_entry.exclusion_kind = ExclusionKind.exclusion
        locked_entry.exclusion_reason = reason
        locked_entry.excluded_at = now_utc()
        locked_entry.excluded_until = None
        if commit:
            await db.commit()
            run = await get_run(db, run.id)
        return run

    async def restore_device_to_run(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        commit: bool = True,
    ) -> TestRun | None:
        run, entry = await get_device_reservation_with_entry(db, device_id)
        if run is None or entry is None:
            return None
        if entry.excluded_until is not None and entry.excluded_until > now_utc():
            return run
        if not _reservation_entry_is_excluded(entry):
            return run
        locked_entry = await _lock_active_reservation_entry(db, entry)
        if locked_entry is None:
            if commit:
                await db.commit()
            return run
        locked_entry.excluded = False
        locked_entry.exclusion_kind = None
        locked_entry.exclusion_reason = None
        locked_entry.excluded_at = None
        locked_entry.excluded_until = None
        locked_entry.cooldown_count = 0
        try:
            device = await device_locking.lock_device(db, device_id, load_sessions=False)
        except NoResultFound, AttributeError:
            # AttributeError reaches us only from in-process unit tests that
            # stub the session with a Fake that has no ``execute``. Production
            # callers always pass a real AsyncSession.
            device = None
        if device is not None:
            await self._review.clear_review_required(
                db,
                device,
                reason="Reservation restored to run",
                source="restore_device_to_run",
            )
        if commit:
            await db.commit()
            run = await get_run(db, run.id)
        return run

    async def exclude_locked_reservation(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        reservation_id: uuid.UUID,
        *,
        reason: str,
    ) -> bool:
        locked.assert_active(db)
        entry = await _lock_active_reservation_entry_by_id(db, reservation_id)
        if entry is None:
            return False
        if _reservation_entry_is_excluded(entry) and entry.exclusion_reason == reason:
            return False
        entry.excluded = True
        entry.exclusion_kind = ExclusionKind.exclusion
        entry.exclusion_reason = reason
        entry.excluded_at = now_utc()
        entry.excluded_until = None
        return True

    async def restore_locked_reservation(
        self,
        db: AsyncSession,
        locked: LockedDevice,
        reservation_id: uuid.UUID,
    ) -> bool:
        locked.assert_active(db)
        entry = await _lock_active_reservation_entry_by_id(db, reservation_id)
        if entry is None or (entry.excluded_until is not None and entry.excluded_until > now_utc()):
            return False
        if not _reservation_entry_is_excluded(entry):
            return False
        entry.excluded = False
        entry.exclusion_kind = None
        entry.exclusion_reason = None
        entry.excluded_at = None
        entry.excluded_until = None
        entry.cooldown_count = 0
        await self._review.clear_review_required(
            db,
            locked.device,
            reason="Reservation restored to run",
            source="restore_device_to_run",
        )
        return True

    async def release_device_from_run(
        self,
        db: AsyncSession,
        device_id: uuid.UUID,
        *,
        reason: str,
        publisher: EventPublisher,
        commit: bool = True,
    ) -> TestRun | None:
        """Permanently remove a device from its active run.

        Unlike ``exclude_device_from_run`` (a restorable hold), this releases the
        reservation (``released_at``) so the device can never rejoin the run — runs
        never re-allocate, and the self-heal restore loop only sees active
        reservations — and frees it for other runs to allocate. Revoking the full
        intent set (run-scoped intents, sub-threshold cooldowns, device-keyed
        health-failure) and reconciling tears down the device's Appium node / grid
        routing. The reason is recorded on the released entry for run history; the
        row is left not-excluded so the released⇒not-excluded invariant holds.
        """
        run, entry = await get_device_reservation_with_entry(db, device_id)
        if run is None or entry is None:
            return None
        locked_entry = await _lock_active_reservation_entry(db, entry)
        if locked_entry is None:
            if commit:
                await db.commit()
            return run
        # Set released_at + reason, and clear any pre-existing exclusion so a released
        # row is never `excluded` (invariant `not (released_at and excluded)`) and carries
        # no live excluded_window for the GiST exclusion constraint.
        locked_entry.released_at = now_utc()
        locked_entry.exclusion_reason = reason
        locked_entry.excluded = False
        locked_entry.exclusion_kind = None
        locked_entry.excluded_at = None
        locked_entry.excluded_until = None
        try:
            device = await device_locking.lock_device(db, device_id, load_sessions=False)
        except NoResultFound:
            device = None
        if device is not None:
            # released_at written above; run: routing and cooldown denies derive from the
            # reservation row, so reconcile to tear them down (no stored release intents now).
            await IntentService(db).reconcile_now(device.id, publisher=publisher)
        if commit:
            await db.commit()
            run = await get_run(db, run.id)
        return run


def reservation_entry_is_excluded(entry: DeviceReservation | None) -> bool:
    return bool(entry and _reservation_entry_is_excluded(entry))


def reservation_gating_run_id(reservation_run: TestRun | None, device_id: uuid.UUID) -> uuid.UUID | None:
    """The run a reservation gates *device_id* to for an arbitrary ticket, or ``None``
    when the device is free for any ticket (no reservation, terminal run, or excluded
    entry).

    Single source for both the grid allocator's reservation gate
    (called directly in ``app.grid.allocation``) and the read-side allocatability
    projection (``app.devices.services.allocatability``), so a UI "reserved"/
    ``allocatable`` signal cannot contradict what the allocator actually refuses.
    """
    if reservation_run is None or reservation_run.state in TERMINAL_STATES:
        return None
    entry = get_reservation_entry_for_device(reservation_run, device_id)
    if reservation_entry_is_excluded(entry):
        return None
    return reservation_run.id


def reservation_gating_owner_sql(*, now: datetime) -> ColumnElement[object]:
    """Correlated scalar subquery for ``Device`` selects: the run id gating this
    device, or NULL when the device is free (no active reservation, terminal run,
    or excluded entry). SQL twin of :func:`reservation_gating_run_id` so the
    grid allocator's batch eligible-devices query and lock-time predicate can
    fold the reservation gate into one read without drifting from the Python
    projection consumed by the read-side allocatability badge.
    """
    return cast(
        "ColumnElement[object]",
        (
            select(DeviceReservation.run_id)
            .join(TestRun, TestRun.id == DeviceReservation.run_id)
            .where(
                DeviceReservation.device_id == Device.id,
                reservation_active(),
                TestRun.state.notin_(TERMINAL_STATES),
                or_(
                    DeviceReservation.excluded.is_(False),
                    and_(
                        DeviceReservation.excluded.is_(True),
                        DeviceReservation.excluded_until.is_not(None),
                        DeviceReservation.excluded_until <= now,
                    ),
                ),
            )
            .order_by(DeviceReservation.created_at.desc())
            .limit(1)
            .correlate(Device)
            .scalar_subquery()
        ),
    )
