from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.observability import get_logger
from app.devices.models import Device

if TYPE_CHECKING:
    import uuid

    from app.core.type_defs import SessionFactory
    from app.devices.protocols import PackDevicePropertiesProvider

logger = get_logger(__name__)


class PropertyRefreshService:
    def __init__(self, *, discovery: PackDevicePropertiesProvider) -> None:
        self._discovery = discovery

    async def fold_host_device_properties(
        self, session_factory: SessionFactory, host_id: uuid.UUID, section: dict[str, Any]
    ) -> None:
        """Fold the pushed device_properties section. The entry mirrors the old
        dial response, so it feeds apply_pack_device_properties verbatim
        (identity guard for network-device connection_target rewrites included).

        Inventory reads in one short session; each device then settles in its
        own fresh transaction, so one failed device cannot poison a peer.

        The ``begin()`` below is the boundary ``apply_pack_device_properties``
        gave up when it became flush-only. Without it the provider's mutation
        would be discarded at session close and every refreshed property would
        be silently dropped.
        """
        raw = section.get("devices")
        if not isinstance(raw, dict) or not raw:
            return
        async with session_factory() as db:
            stmt = select(Device.id, Device.connection_target).where(
                Device.host_id == host_id, Device.connection_target.in_(list(raw))
            )
            targets = (await db.execute(stmt)).all()

        # Immutable (device_id, connection_target, data) work, detached from the
        # inventory session: no ORM row crosses into per-device settlement.
        work: list[tuple[uuid.UUID, str, dict[str, Any]]] = []
        for device_id, target in targets:
            data = raw.get(target)
            if isinstance(data, dict):
                work.append((device_id, target, copy.deepcopy(data)))

        for device_id, _target, data in sorted(work, key=lambda item: str(item[0])):
            try:
                async with session_factory.begin() as db:
                    device = await db.get(Device, device_id, options=[selectinload(Device.host)])
                    if device is None:
                        continue
                    await self._discovery.apply_pack_device_properties(db, device, data)
            except Exception:
                logger.exception("Failed to fold refreshed properties for device %s", device_id)
