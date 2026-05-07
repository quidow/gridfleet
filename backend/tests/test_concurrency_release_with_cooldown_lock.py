"""Two parallel release-with-cooldown calls on the same (run, device) must
serialize on the DeviceReservation row lock.  The second call sees
``claimed_by == None`` and raises the documented ``not claimed`` ValueError.

Also covers the reassignment-race guard: when the old reservation is released
between Tx1 commit and the escalation phase, enter_maintenance must NOT be
called and the response still signals ``maintenance_escalated``.
"""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import DeviceHold, DeviceOperationalState
from app.models.device_reservation import DeviceReservation
from app.services import run_service
from app.services.settings_service import settings_service
from tests.helpers import create_device, create_reserved_run

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_parallel_release_with_cooldown_serializes(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: object,
) -> None:
    """Two concurrent callers for the same (run, device) must serialize.

    The DeviceReservation row lock guarantees that exactly one caller wins
    (clears the claim and sets the cooldown) while the other observes
    ``claimed_by == None`` and raises ValueError("not claimed").
    """
    # Disable escalation so neither call triggers the maintenance path.
    settings_service._cache["general.device_cooldown_escalation_threshold"] = 0

    from app.models.host import Host

    host = db_host
    assert isinstance(host, Host)

    device = await create_device(
        db_session,
        host_id=host.id,
        name="cooldown-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    await db_session.refresh(device)

    worker_id = "gw0"
    run = await create_reserved_run(
        db_session,
        name="cooldown-race-run",
        devices=[device],
        claimed_device_ids={str(device.id): worker_id},
    )

    run_id = run.id
    device_id = device.id

    errors: list[ValueError] = []

    async def call_release() -> bool:
        """Return True on success, append error and return False on ValueError."""
        async with db_session_maker() as session:
            try:
                await run_service.release_claimed_device_with_cooldown(
                    session,
                    run_id,
                    device_id=device_id,
                    worker_id=worker_id,
                    reason="race",
                    ttl_seconds=60,
                )
                return True
            except ValueError as exc:
                errors.append(exc)
                return False

    results = await asyncio.gather(call_release(), call_release(), return_exceptions=False)

    winners = sum(1 for r in results if r is True)
    losers = sum(1 for r in results if r is False)

    assert winners == 1, f"Expected exactly 1 winner, got {winners} (results={results})"
    assert losers == 1, f"Expected exactly 1 loser, got {losers} (results={results})"

    assert len(errors) == 1
    assert "not claimed" in str(errors[0]).lower(), f"Expected 'not claimed' in error message, got: {errors[0]}"


async def test_escalation_skipped_when_device_reassigned_between_tx1_and_escalation(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    db_host: object,
) -> None:
    """Escalation must not call enter_maintenance when the device was reassigned.

    Race scenario:
    1. Tx1 increments cooldown_count and sets excluded=True on the old reservation.
    2. A concurrent complete_run / cancel_run releases the old reservation
       (sets released_at) between Tx1 commit and the escalation phase.
    3. A new run could then reserve the same device.
    4. The escalation phase must detect the mismatch and skip enter_maintenance
       so the new run's device is not put into maintenance.

    Expected outcome:
    - No maintenance hold on the device.
    - Response tuple has escalate=True (the OLD reservation IS permanently
      excluded) and device_hold=None.
    """
    # Set threshold=1 so a single cooldown triggers escalation.
    settings_service._cache["general.device_cooldown_escalation_threshold"] = 1

    from app.models.host import Host

    host = db_host
    assert isinstance(host, Host)

    device = await create_device(
        db_session,
        host_id=host.id,
        name="cooldown-reassign-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    await db_session.commit()
    await db_session.refresh(device)

    worker_id = "gw0"
    run = await create_reserved_run(
        db_session,
        name="cooldown-reassign-run",
        devices=[device],
        claimed_device_ids={str(device.id): worker_id},
    )
    run_id = run.id
    device_id = device.id

    # Simulate the race: run Tx1 phase (claim + cooldown_count increment) then
    # release the reservation BEFORE the escalation phase checks it.
    #
    # We do this by monkey-patching run_reservation_service so the first call
    # to get_device_reservation_with_entry (the reassignment check inside the
    # escalation phase) returns (None, None), simulating the old reservation
    # having been released and no new reservation existing yet.
    import app.services.run_reservation_service as rrs_mod

    original_get = rrs_mod.get_device_reservation_with_entry

    call_count = 0

    async def patched_get(
        db: AsyncSession,
        did: uuid.UUID,
    ) -> tuple[object, object]:
        nonlocal call_count
        call_count += 1
        # The first call inside the escalation phase should return nothing to
        # simulate the device having been released and reassigned.
        if call_count == 1:
            # Also actually release the old reservation in DB to make state
            # consistent with what the patched return value implies.
            reservation = (
                await db.execute(
                    select(DeviceReservation)
                    .where(
                        DeviceReservation.run_id == run_id,
                        DeviceReservation.device_id == device_id,
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if reservation is not None:
                from datetime import UTC, datetime

                reservation.released_at = datetime.now(UTC)
                await db.commit()
            return None, None
        return await original_get(db, did)

    rrs_mod.get_device_reservation_with_entry = patched_get  # type: ignore[assignment]
    try:
        async with db_session_maker() as session:
            (
                _reservation_payload,
                _next_op_state,
                device_hold,
                excluded_until,
                cooldown_count_after,
                escalated,
                _threshold_out,
            ) = await run_service.release_claimed_device_with_cooldown(
                session,
                run_id,
                device_id=device_id,
                worker_id=worker_id,
                reason="flake",
                ttl_seconds=60,
            )
    finally:
        rrs_mod.get_device_reservation_with_entry = original_get  # type: ignore[assignment]

    # The OLD reservation was escalated (escalate=True) but no maintenance hold
    # should have been applied because the device was reassigned.
    assert escalated is True, "escalated flag must be True — the old reservation IS permanently excluded"
    assert device_hold is None, "device_hold must be None — enter_maintenance was skipped"
    assert excluded_until is None, "excluded_until must be None for the permanent-exclusion path"
    assert cooldown_count_after == 1

    # The physical device must NOT be in maintenance hold.
    await db_session.refresh(device)
    assert device.hold != DeviceHold.maintenance, (
        f"Device must not be in maintenance hold after a reassignment-abort; got hold={device.hold}"
    )
