"""Build a portable JSON bundle of registered devices for round-trip export/import.

The bundle carries only operator-configured fields and identity, not runtime
state. Hardware-detected fields (``os_version``, ``manufacturer``, ``model``,
``software_versions``) are deliberately excluded — they are re-discovered by
the verification pipeline after a device is re-imported.
"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.devices.models import Device
from app.portability.schemas import (
    SCHEMA_VERSION,
    ExportBundle,
    ExportedDevice,
    OriginalHost,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PortabilityExportService:
    async def build_export_bundle(self, db: AsyncSession) -> ExportBundle:
        stmt = select(Device).options(selectinload(Device.host)).order_by(Device.created_at.asc())
        result = await db.execute(stmt)
        devices = result.scalars().all()
        exported = [_exported_device(d) for d in devices]
        return ExportBundle(
            schema_version=SCHEMA_VERSION,
            exported_at=datetime.now(UTC),
            source_instance=None,
            devices=exported,
        )


def _exported_device(d: Device) -> ExportedDevice:
    host = d.host
    if host is None:
        raise RuntimeError(f"Device {d.id} has no associated host loaded — check selectinload")
    identity_scope = d.identity_scope
    if identity_scope not in ("global", "host"):
        raise ValueError(f"Unexpected identity_scope {identity_scope!r} for device {d.id}")
    return ExportedDevice(
        pack_id=d.pack_id,
        platform_id=d.platform_id,
        identity_scheme=d.identity_scheme,
        identity_scope=identity_scope,
        identity_value=d.identity_value,
        name=d.name,
        device_type=d.device_type,
        connection_type=d.connection_type,
        connection_target=d.connection_target,
        tags=dict(d.tags or {}),
        device_config=dict(d.device_config or {}),
        test_data=dict(d.test_data or {}),
        original_host=OriginalHost(hostname=host.hostname, host_id=host.id),
    )
