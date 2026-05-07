"""Two parallel release-with-cooldown calls on the same (run, device) must
serialize on the DeviceReservation row lock.  The second call sees
``claimed_by == None`` and raises the documented ``not claimed`` ValueError.
"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import DeviceOperationalState
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
