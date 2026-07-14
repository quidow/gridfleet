import uuid
from typing import TYPE_CHECKING

from app.core.observation_revision import next_observation_revision
from app.devices.services.health import DeviceHealthService
from tests.helpers import seed_host_and_device
from tests.helpers import test_event_bus as event_bus

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def test_device_health_failure_episode_lifecycle(db_session: AsyncSession) -> None:
    _host, device = await seed_host_and_device(db_session, identity="failure-episode")
    service = DeviceHealthService(publisher=event_bus)
    stale_revision = await next_observation_revision(db_session)

    assert device.device_checks_healthy is None
    assert device.failure_episode_id is None

    assert await service.update_device_checks(db_session, device, healthy=False, summary="Disconnected") is True
    await db_session.commit()
    await db_session.refresh(device)
    first_episode = device.failure_episode_id
    assert device.device_checks_healthy is False
    assert isinstance(first_episode, uuid.UUID)

    assert (
        await service.update_device_checks(
            db_session,
            device,
            healthy=True,
            summary="Stale recovery",
            revision=stale_revision,
        )
        is False
    )
    await db_session.commit()
    await db_session.refresh(device)
    assert device.device_checks_healthy is False
    assert device.failure_episode_id == first_episode

    assert await service.update_device_checks(db_session, device, healthy=False, summary="Still disconnected") is True
    await db_session.commit()
    await db_session.refresh(device)
    assert device.failure_episode_id == first_episode

    assert await service.update_device_checks(db_session, device, healthy=True, summary="Healthy") is True
    await db_session.commit()
    await db_session.refresh(device)
    assert device.failure_episode_id is None

    assert await service.update_device_checks(db_session, device, healthy=False, summary="Disconnected again") is True
    await db_session.commit()
    await db_session.refresh(device)
    assert isinstance(device.failure_episode_id, uuid.UUID)
    assert device.failure_episode_id != first_episode
