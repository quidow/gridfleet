import asyncio

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import get_pack_device_properties
from app.core.database import async_session
from app.core.observability import get_logger, observe_background_loop
from app.devices.models import Device, DeviceOperationalState
from app.hosts.models import Host, HostStatus
from app.packs.services import discovery as pack_discovery
from app.settings import settings_service

pack_refresh_device_properties = pack_discovery.refresh_device_properties

logger = get_logger(__name__)
LOOP_NAME = "property_refresh"


async def _refresh_all_properties() -> None:
    async with async_session() as db:
        host_result = await db.execute(select(Host).where(Host.status == HostStatus.online))
        online_host_ids = [host.id for host in host_result.scalars().all()]
        if not online_host_ids:
            return

        device_stmt = (
            select(Device)
            .where(
                Device.host_id.in_(online_host_ids),
                Device.operational_state != DeviceOperationalState.offline,
            )
            .options(selectinload(Device.host))
        )
        device_result = await db.execute(device_stmt)
        devices = list(device_result.scalars().all())
        if not devices:
            return

        async def _fetch(device: Device) -> tuple[Device, dict[str, object] | None]:
            host = device.host
            if host is None:
                return device, None
            try:
                data = await pack_discovery.fetch_pack_device_properties(
                    host, device, agent_get_pack_device_properties=get_pack_device_properties
                )
            except Exception:
                logger.exception("Failed to fetch properties for device %s", device.identity_value)
                return device, None
            return device, data

        results = await asyncio.gather(*(_fetch(device) for device in devices))
        for device, data in results:
            if data is None:
                continue
            try:
                await pack_discovery.apply_pack_device_properties(db, device, data)
            except Exception:
                logger.exception("Failed to apply refreshed properties for device %s", device.identity_value)


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
