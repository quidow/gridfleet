"""Bug 10: ``exclude_device_from_run`` marks a just-released reservation as excluded.

See ``docs/superpowers/specs/2026-05-20-backend-bug-audit.md#bug-10``.

``exclude_device_from_run`` at
``backend/app/runs/service_reservation_lookup.py:89-120`` reads the
active reservation via
``get_device_reservation_with_entry`` *without* a lock at line 96,
mutates ``entry.excluded = True`` (line 102-105) on the in-memory ORM
object, and *then* locks the device at line 107. Between the unlocked
read and the device lock, a concurrent ``_release_devices`` can land
``reservation.released_at = NOW``. When our session finally flushes,
the reservation row ends up with **both** ``released_at`` set
(released) AND ``excluded = True`` (excluded) — a contradiction the
release path is supposed to make impossible.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

import app.runs.service_reservation as _reservation
from app.devices.models import DeviceOperationalState, DeviceReservation
from app.runs.models import RunState, TestRun
from tests.fakes import build_review_service
from tests.helpers import create_device, create_host

if TYPE_CHECKING:
    from httpx2 import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.mark.db
@pytest.mark.asyncio
async def test_exclude_marks_released_reservation_as_excluded(
    db_session: AsyncSession,
    db_session_maker: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    host = await create_host(client)
    device = await create_device(
        db_session,
        host_id=uuid.UUID(host["id"]),
        name="exclude-after-release",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    run = TestRun(
        name=f"race-{uuid.uuid4().hex[:6]}",
        state=RunState.active,
        requirements=[{"platform_id": device.platform_id, "count": 1}],
        ttl_minutes=60,
        heartbeat_timeout_sec=120,
        last_heartbeat=None,
    )
    db_session.add(run)
    await db_session.flush()
    reservation = DeviceReservation(
        run_id=run.id,
        device_id=device.id,
        identity_value=device.identity_value,
        connection_target=device.connection_target,
        pack_id=device.pack_id,
        platform_id=device.platform_id,
        os_version=device.os_version,
    )
    db_session.add(reservation)
    await db_session.commit()
    device_id = device.id
    reservation_id = reservation.id

    original_get = _reservation.get_device_reservation_with_entry

    async def _get_then_release(
        db: Any,  # noqa: ANN401
        did: uuid.UUID,
    ) -> Any:  # noqa: ANN401
        # Race: between exclude_device_from_run's unlocked read of the
        # reservation here and any locked re-fetch / device lock, a
        # concurrent _release_devices commits reservation.released_at = NOW.
        # Drive that side-channel commit right after the unlocked snapshot
        # is captured so the caller proceeds with a stale entry.
        snapshot = await original_get(db, did)
        async with db_session_maker() as side:
            row = await side.get(DeviceReservation, reservation_id)
            if row is not None and row.released_at is None:
                row.released_at = datetime.now(UTC)
                await side.commit()
        return snapshot

    with patch.object(_reservation, "get_device_reservation_with_entry", side_effect=_get_then_release):
        await _reservation.RunReservationService(review=build_review_service()).exclude_device_from_run(
            db_session, device_id, reason="test race", commit=True
        )

    # Re-read the reservation on a fresh session.
    async with db_session_maker() as side:
        refreshed = (
            await side.execute(select(DeviceReservation).where(DeviceReservation.id == reservation_id))
        ).scalar_one()

    # Fixed behavior: exclude_device_from_run would re-fetch the
    # reservation under FOR UPDATE (or a `WHERE released_at IS NULL`
    # guard on its UPDATE) and bail when the reservation has already
    # been released. Current behavior (bug): a released reservation
    # ends up with excluded=True — a contradictory state the release
    # path is supposed to prevent.
    assert not (refreshed.released_at is not None and refreshed.excluded), (
        f"reservation has contradictory state: released_at={refreshed.released_at}, "
        f"excluded={refreshed.excluded}, exclusion_reason={refreshed.exclusion_reason!r}"
    )
