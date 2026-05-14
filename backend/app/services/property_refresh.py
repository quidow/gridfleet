import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models.device import Device, DeviceOperationalState
from app.models.host import Host, HostStatus
from app.observability import get_logger, observe_background_loop
from app.packs.services import discovery as pack_discovery
from app.services.agent_operations import get_pack_device_properties
from app.settings import settings_service

pack_refresh_device_properties = pack_discovery.refresh_device_properties

logger = get_logger(__name__)
LOOP_NAME = "property_refresh"


async def _refresh_all_properties() -> None:
    async with async_session() as db:
        stmt = select(Host).where(Host.status == HostStatus.online)
        result = await db.execute(stmt)
        hosts = result.scalars().all()

        for host in hosts:
            device_stmt = (
                select(Device)
                .where(Device.host_id == host.id, Device.operational_state != DeviceOperationalState.offline)
                .options(selectinload(Device.host))
            )
            device_result = await db.execute(device_stmt)
            devices = device_result.scalars().all()

            for device in devices:
                try:
                    await pack_refresh_device_properties(
                        db,
                        device,
                        agent_get_pack_device_properties=get_pack_device_properties,
                    )
                except Exception:
                    logger.exception("Failed to refresh properties for device %s", device.identity_value)


async def property_refresh_loop() -> None:
    """Background loop that periodically refreshes device properties."""
    while True:
        interval = float(settings_service.get("general.property_refresh_interval_sec"))
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle():
                await _refresh_all_properties()
        except Exception:
            logger.exception("Property refresh cycle failed")
        await asyncio.sleep(interval)
