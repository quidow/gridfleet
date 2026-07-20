"""The Python reservation projection and its SQL twin must agree per device.

``get_device_reservation_map`` + ``reservation_gating_run_id`` (read-side badge,
group-membership facts) and ``reservation_gating_owner_sql`` (grid allocator's
eligible-devices projection and claim-time predicate) are documented as one
source of truth. They are only equivalent because a device can carry at most one
*active* reservation row: ``get_device_reservation_map`` picks an arbitrary
active row per device and lets ``reservation_gating_run_id`` nullify it
afterwards, whereas the SQL twin filters terminal/excluded rows first and then
takes the newest. With two active rows those orders disagree.

``uq_device_reservations_active_device`` (partial unique index on ``device_id``
WHERE ``released_at IS NULL``) is what makes that state unreachable, so it is
pinned here alongside the agreement itself: if it is ever dropped, the two
projections silently diverge and this file fails first.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceOperationalState, DeviceReservation
from app.runs.models import RunState, TestRun
from app.runs.service_reservation import (
    get_device_reservation_map,
    reservation_gating_owner_sql,
    reservation_gating_run_id,
)
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [pytest.mark.asyncio, pytest.mark.db, pytest.mark.usefixtures("seeded_driver_packs")]


async def _run(db_session: AsyncSession, state: RunState) -> TestRun:
    run = TestRun(
        name=f"gating-{state.value}-{uuid.uuid4().hex[:6]}",
        state=state,
        requirements=[],
        ttl_minutes=10,
        heartbeat_timeout_sec=300,
    )
    db_session.add(run)
    await db_session.flush()
    return run


def _reservation(device: Device, run: TestRun, **overrides: object) -> DeviceReservation:
    return DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
        **overrides,  # type: ignore[arg-type]
    )


async def _sql_gating_owner(db_session: AsyncSession, device: Device) -> uuid.UUID | None:
    owner = (
        await db_session.execute(select(reservation_gating_owner_sql(now=now_utc())).where(Device.id == device.id))
    ).scalar_one_or_none()
    return None if owner is None else uuid.UUID(str(owner))


async def _python_gating_owner(db_session: AsyncSession, device: Device) -> uuid.UUID | None:
    reservation_map = await get_device_reservation_map(db_session, [device.id])
    return reservation_gating_run_id(reservation_map.get(device.id), device.id)


async def test_two_active_reservations_on_one_device_are_rejected(
    db_session: AsyncSession, default_host_id: str
) -> None:
    """The only state in which the two projections could disagree is refused by
    the database, which is why they can be treated as one source of truth."""
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="gating-dup",
        operational_state=DeviceOperationalState.available,
    )
    terminal = await _run(db_session, RunState.completed)
    live = await _run(db_session, RunState.active)
    db_session.add_all([_reservation(device, terminal), _reservation(device, live)])
    with pytest.raises(IntegrityError, match="uq_device_reservations_active_device"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize(
    ("state", "excluded", "gated"),
    [
        (RunState.active, False, True),
        (RunState.preparing, False, True),
        (RunState.completed, False, False),
        (RunState.cancelled, False, False),
        (RunState.active, True, False),
    ],
)
async def test_badge_and_allocator_agree_on_the_gating_owner(
    db_session: AsyncSession,
    default_host_id: str,
    state: RunState,
    excluded: bool,
    gated: bool,
) -> None:
    """Across every reachable shape — live run, terminal run, indefinitely excluded
    entry — the read-side badge and the allocator's SQL projection must name the
    same owner. A released row for another run must not shift either verdict."""
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name=f"gating-{state.value}-{excluded}",
        operational_state=DeviceOperationalState.available,
    )
    # A stale released row for a different run: invisible to both projections.
    old_run = await _run(db_session, RunState.completed)
    db_session.add(_reservation(device, old_run, released_at=now_utc()))
    run = await _run(db_session, state)
    db_session.add(_reservation(device, run, excluded=excluded, excluded_at=now_utc() if excluded else None))
    await db_session.flush()

    expected = run.id if gated else None
    assert await _python_gating_owner(db_session, device) == expected
    assert await _sql_gating_owner(db_session, device) == expected
