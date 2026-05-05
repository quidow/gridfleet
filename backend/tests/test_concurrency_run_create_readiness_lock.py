import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.device import Device, DeviceOperationalState
from app.models.device_reservation import DeviceReservation
from app.schemas.run import RunCreate
from app.services import device_locking, run_service
from tests.helpers import create_device

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("seeded_driver_packs")]


async def test_create_run_rechecks_readiness_after_lock(
    db_session_maker: async_sessionmaker[AsyncSession],
    db_session: AsyncSession,
    default_host_id: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = await create_device(
        db_session,
        host_id=default_host_id,
        name="readiness-race",
        operational_state=DeviceOperationalState.available,
        verified=True,
    )
    device_id = device.id
    await db_session.commit()

    readiness_checked = asyncio.Event()
    allow_reservation = asyncio.Event()
    original_readiness = run_service._readiness_for_match

    async def gated_readiness(db: AsyncSession, candidate: Device) -> bool:
        result = await original_readiness(db, candidate)
        if candidate.id == device_id:
            readiness_checked.set()
            await asyncio.wait_for(allow_reservation.wait(), timeout=2.0)
        return result

    monkeypatch.setattr(run_service, "_readiness_for_match", gated_readiness)

    async def create_run() -> None:
        async with db_session_maker() as session:
            with pytest.raises(ValueError, match="Not enough devices"):
                await run_service.create_run(
                    session,
                    RunCreate(
                        name="readiness-race-run",
                        requirements=[{"pack_id": device.pack_id, "platform_id": device.platform_id, "count": 1}],
                    ),
                )

    async def clear_verification_after_readiness() -> None:
        await asyncio.wait_for(readiness_checked.wait(), timeout=2.0)
        async with db_session_maker() as session:
            locked = await device_locking.lock_device(session, device_id)
            locked.verified_at = None
            await session.commit()
        allow_reservation.set()

    await asyncio.gather(create_run(), clear_verification_after_readiness())

    async with db_session_maker() as verify:
        reservation = (
            await verify.execute(
                select(DeviceReservation).where(
                    DeviceReservation.device_id == device_id,
                    DeviceReservation.released_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        final_device = (await verify.execute(select(Device).where(Device.id == device_id))).scalar_one()

    assert reservation is None
    assert final_device.operational_state == DeviceOperationalState.available
