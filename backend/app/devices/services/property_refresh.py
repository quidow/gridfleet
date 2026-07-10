from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.observability import get_logger
from app.devices.models import Device

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.devices.protocols import PackDevicePropertiesProvider

logger = get_logger(__name__)


class PropertyRefreshService:
    def __init__(self, *, discovery: PackDevicePropertiesProvider) -> None:
        self._discovery = discovery

    async def fold_host_device_properties(self, db: AsyncSession, host_id: uuid.UUID, section: dict[str, Any]) -> None:
        """Fold the pushed device_properties section. The entry mirrors the old
        dial response, so it feeds apply_pack_device_properties verbatim
        (identity guard for network-device connection_target rewrites included)."""
        raw = section.get("devices")
        if not isinstance(raw, dict) or not raw:
            return
        stmt = select(Device.id, Device.connection_target).where(
            Device.host_id == host_id, Device.connection_target.in_(list(raw))
        )
        # Snapshot ids up front: the per-device commit/rollback below expires
        # every instance in the session, so iterating live ORM rows would trigger
        # a sync lazy-load (MissingGreenlet) after the first rollback. Re-fetch
        # each device fresh inside its own commit window instead.
        targets = [(device_id, target) for device_id, target in (await db.execute(stmt)).all()]
        for device_id, target in targets:
            data = raw.get(target)
            if not isinstance(data, dict):
                continue
            device = await db.get(Device, device_id, options=[selectinload(Device.host)])
            if device is None:
                continue
            try:
                await self._discovery.apply_pack_device_properties(db, device, data)
                await db.commit()
            except Exception:
                await db.rollback()
                logger.exception("Failed to fold refreshed properties for device %s", device_id)
