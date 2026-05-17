from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from app.devices.models import DeviceIntent
from app.devices.services.intent import IntentService
from app.devices.services.intent_types import RESERVATION, IntentRegistration
from app.runs import service as run_service
from tests.helpers import create_device, create_reserved_run

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.hosts.models import Host


async def _seed_health_failure_reservation_intent(
    db_session: AsyncSession,
    *,
    device_id: object,
    run_id: object,
    reason: str = "stale exclusion text",
) -> None:
    service = IntentService(db_session)
    await service.register_intents(
        device_id=device_id,
        reason="seed health failure exclusion",
        intents=[
            IntentRegistration(
                source=f"health_failure:reservation:{device_id}",
                axis=RESERVATION,
                run_id=run_id,
                payload={
                    "excluded": True,
                    "priority": 60,
                    "exclusion_reason": reason,
                },
            )
        ],
    )
    await db_session.commit()


async def _intent_exists(db_session: AsyncSession, *, device_id: object, source: str) -> bool:
    result = await db_session.execute(
        select(DeviceIntent.id).where(
            DeviceIntent.device_id == device_id,
            DeviceIntent.source == source,
        )
    )
    return result.scalar_one_or_none() is not None


async def test_cancel_run_revokes_health_failure_reservation_intent(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="cancel-release")
    run = await create_reserved_run(db_session, name="cancel-release-run", devices=[device])
    await _seed_health_failure_reservation_intent(db_session, device_id=device.id, run_id=run.id)
    assert await _intent_exists(
        db_session,
        device_id=device.id,
        source=f"health_failure:reservation:{device.id}",
    )

    await run_service.cancel_run(db_session, run.id)

    assert not await _intent_exists(
        db_session,
        device_id=device.id,
        source=f"health_failure:reservation:{device.id}",
    )


async def test_complete_run_revokes_health_failure_reservation_intent(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="complete-release")
    run = await create_reserved_run(db_session, name="complete-release-run", devices=[device])
    await _seed_health_failure_reservation_intent(db_session, device_id=device.id, run_id=run.id)

    await run_service.complete_run(db_session, run.id)

    assert not await _intent_exists(
        db_session,
        device_id=device.id,
        source=f"health_failure:reservation:{device.id}",
    )


async def test_expire_run_revokes_health_failure_reservation_intent(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="expire-release")
    run = await create_reserved_run(db_session, name="expire-release-run", devices=[device])
    await _seed_health_failure_reservation_intent(db_session, device_id=device.id, run_id=run.id)

    await run_service.expire_run(db_session, run, "Heartbeat timeout")

    assert not await _intent_exists(
        db_session,
        device_id=device.id,
        source=f"health_failure:reservation:{device.id}",
    )
