import logging
import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.exc import NoResultFound
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.devices import locking as device_locking
from app.devices import schemas as device_schemas
from app.devices.models import DeviceReservation
from app.devices.services import (
    capability,
    intent_types,
    lifecycle_incidents,
    maintenance,
    platform_label,
    readiness,
    state,
)
from app.devices.services import (
    intent as intent_service,
)
from app.packs.services import platform_resolver as pack_platform_resolver
from app.runs import service_reservation as run_reservation_service
from app.runs.models import TestRun
from app.runs.service_query import get_run
from app.runs.service_time import now_utc

assert_runnable = pack_platform_resolver.assert_runnable
GRID_ROUTING = intent_types.GRID_ROUTING
NODE_PROCESS = intent_types.NODE_PROCESS
PRIORITY_COOLDOWN = intent_types.PRIORITY_COOLDOWN
PRIORITY_FORCED_RELEASE = intent_types.PRIORITY_FORCED_RELEASE
PRIORITY_RUN_ROUTING = intent_types.PRIORITY_RUN_ROUTING
RECOVERY = intent_types.RECOVERY
RESERVATION = intent_types.RESERVATION
IntentRegistration = intent_types.IntentRegistration
DeviceLifecyclePolicySummaryState = device_schemas.DeviceLifecyclePolicySummaryState
is_ready_for_use_async = readiness.is_ready_for_use_async
ready_operational_state = state.ready_operational_state
set_hold = state.set_hold
set_operational_state = state.set_operational_state
capability_service = capability
register_intents_and_reconcile = intent_service.register_intents_and_reconcile
revoke_intents_and_reconcile = intent_service.revoke_intents_and_reconcile
lifecycle_incident_service = lifecycle_incidents
maintenance_service = maintenance
platform_label_service = platform_label

logger = logging.getLogger(__name__)


def _reserved_entry_is_excluded(entry: DeviceReservation) -> bool:
    if not entry.excluded:
        return False
    if entry.excluded_until is None:
        return True
    return entry.excluded_until > now_utc()


def _reserved_entry_matches(entry: DeviceReservation, device_id: uuid.UUID | str) -> bool:
    return str(entry.device_id) == str(device_id)


def _reserved_entry_for_device(
    run: TestRun,
    device_id: uuid.UUID | str,
    *,
    active_only: bool = False,
) -> DeviceReservation | None:
    if not run.device_reservations:
        return None

    matching = [entry for entry in run.device_reservations if _reserved_entry_matches(entry, device_id)]
    if active_only:
        matching = [entry for entry in matching if entry.released_at is None]
    if not matching:
        return None
    return cast("DeviceReservation", matching[-1])


async def get_device_reservation_with_entry(
    db: AsyncSession,
    device_id: uuid.UUID,
) -> tuple[TestRun | None, DeviceReservation | None]:
    return await run_reservation_service.get_device_reservation_with_entry(db, device_id)


async def get_device_reservation_map(db: AsyncSession, device_ids: list[uuid.UUID]) -> dict[uuid.UUID, TestRun]:
    if not device_ids:
        return {}

    stmt = (
        select(DeviceReservation)
        .where(DeviceReservation.device_id.in_(device_ids), DeviceReservation.released_at.is_(None))
        .options(selectinload(DeviceReservation.run).selectinload(TestRun.device_reservations))
    )
    result = await db.execute(stmt)
    reservation_map: dict[uuid.UUID, TestRun] = {}
    for reservation in result.scalars().all():
        reservation_map[reservation.device_id] = reservation.run
    return reservation_map


def get_reservation_entry_for_device(run: TestRun, device_id: uuid.UUID | str) -> DeviceReservation | None:
    return _reserved_entry_for_device(run, device_id, active_only=True)


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
    if _reserved_entry_is_excluded(entry) and entry.exclusion_reason == reason:
        return run

    entry.excluded = True
    entry.exclusion_reason = reason
    entry.excluded_at = datetime.now(UTC)
    entry.excluded_until = None
    try:
        device = await device_locking.lock_device(db, device_id, load_sessions=False)
    except NoResultFound:
        device = None
    if device is not None:
        await revoke_intents_and_reconcile(
            db,
            device_id=device.id,
            sources=[f"run:{run.id}"],
            reason=reason,
        )
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
    if entry.excluded_until is not None and entry.excluded_until > now_utc():
        return run
    if not _reserved_entry_is_excluded(entry):
        return run

    entry.excluded = False
    entry.exclusion_reason = None
    entry.excluded_at = None
    entry.excluded_until = None
    if commit:
        await db.commit()
        run = await get_run(db, run.id)
    return run


def reservation_entry_is_excluded(entry: DeviceReservation | None) -> bool:
    return run_reservation_service.reservation_entry_is_excluded(entry)
