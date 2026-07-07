from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.background_loop import BackgroundLoop
from app.core.observability import get_logger
from app.devices.models import Device, DeviceOperationalState
from app.hosts.models import Host, HostStatus

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SessionFactory
    from app.devices.protocols import PackDevicePropertiesProvider
    from app.devices.services_container import DeviceServices

logger = get_logger(__name__)
LOOP_NAME = "property_refresh"

# Cap simultaneous host fetches so a large fleet does not fan out unbounded HTTP load.
MAX_PARALLEL_HOST_FETCHES = 8


class PropertyRefreshService:
    def __init__(self, *, discovery: PackDevicePropertiesProvider) -> None:
        self._discovery = discovery

    async def refresh_all_properties(self, db: AsyncSession) -> None:
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

        # Parallelize across hosts but keep requests to a single agent sequential.
        # The shared `db` session is not used inside `_fetch_host`, which keeps the
        # gather safe; all DB writes happen after the gather completes.
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
                        data = await self._discovery.fetch_pack_device_properties(host, device)
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
                await self._discovery.apply_pack_device_properties(db, device, data)
            except Exception:
                logger.exception("Failed to apply refreshed properties for device %s", device.identity_value)
                await db.rollback()


class PropertyRefreshLoop(BackgroundLoop):
    """Background loop that periodically refreshes device properties."""

    loop_name = LOOP_NAME
    cycle_failed_message = "Property refresh cycle failed"

    def __init__(self, *, services: DeviceServices) -> None:
        self._services = services

    @property
    def _session_factory(self) -> SessionFactory:
        return self._services.session_factory

    def _interval(self) -> float:
        return self._services.settings.get_float("general.property_refresh_interval_sec")

    async def _run_cycle(self, db: AsyncSession) -> None:
        await self._services.property_refresh.refresh_all_properties(db)
