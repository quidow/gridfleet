from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agent_comm.operations import get_pack_device_properties
from app.core.database import async_session
from app.core.observability import get_logger, observe_background_loop
from app.devices.models import Device, DeviceOperationalState
from app.hosts.models import Host, HostStatus
from app.packs.services import discovery as pack_discovery
from app.settings import settings_service

if TYPE_CHECKING:
    import uuid

logger = get_logger(__name__)
LOOP_NAME = "property_refresh"

# Cap simultaneous host fetches so a large fleet does not fan out unbounded HTTP load.
MAX_PARALLEL_HOST_FETCHES = 8


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

        # Parallelize across hosts but keep requests to a single agent sequential —
        # the original loop processed one device at a time per host. The shared `db`
        # session is not used inside `_fetch_host`, which keeps the gather safe; all
        # DB writes happen after the gather completes.
        devices_by_host: dict[uuid.UUID, list[Device]] = defaultdict(list)
        for device in devices:
            devices_by_host[device.host_id].append(device)

        semaphore = asyncio.Semaphore(MAX_PARALLEL_HOST_FETCHES)

        async def _fetch_host(host_devices: list[Device]) -> list[tuple[Device, dict[str, object] | None]]:
            async with semaphore:
                host_results: list[tuple[Device, dict[str, object] | None]] = []
                for device in host_devices:
                    host = device.host
                    if host is None:
                        host_results.append((device, None))
                        continue
                    try:
                        data = await pack_discovery.fetch_pack_device_properties(
                            host, device, agent_get_pack_device_properties=get_pack_device_properties
                        )
                    except Exception:
                        logger.exception("Failed to fetch properties for device %s", device.identity_value)
                        host_results.append((device, None))
                        continue
                    host_results.append((device, data))
                return host_results

        host_results = await asyncio.gather(*(_fetch_host(host_devices) for host_devices in devices_by_host.values()))
        for device, data in (entry for host_batch in host_results for entry in host_batch):
            if data is None:
                continue
            try:
                await pack_discovery.apply_pack_device_properties(db, device, data)
            except Exception:
                logger.exception("Failed to apply refreshed properties for device %s", device.identity_value)
                await db.rollback()


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
