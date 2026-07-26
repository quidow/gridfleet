"""Build a portable JSON bundle of registered devices for round-trip export/import.

The bundle carries only operator-configured fields and identity, not runtime
state. Hardware-detected fields (``os_version``, ``manufacturer``, ``model``,
``software_versions``) are deliberately excluded — they are re-discovered by
the verification pipeline after a device is re-imported.
"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.timeutil import now_utc
from app.devices.models import Device, DeviceGroup, GroupType
from app.devices.services.group_membership import load_member_of_keys, load_static_group_keys_by_device_id
from app.portability.schemas import (
    SCHEMA_VERSION,
    ExportBundle,
    ExportedDevice,
    ExportedDeviceGroup,
    OriginalHost,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class PortabilityExportService:
    async def build_export_bundle(self, db: AsyncSession) -> ExportBundle:
        stmt = select(Device).options(selectinload(Device.host)).order_by(Device.created_at.asc())
        result = await db.execute(stmt)
        devices = list(result.scalars().all())

        groups_stmt = select(DeviceGroup).order_by(DeviceGroup.key.asc())
        groups_result = await db.execute(groups_stmt)
        groups = list(groups_result.scalars().all())
        dynamic_group_ids = [g.id for g in groups if g.group_type == GroupType.dynamic]
        member_of_keys_by_dynamic_group_id = await load_member_of_keys(db, dynamic_group_ids)
        exported_groups = [
            _exported_group(g, member_of_keys_by_dynamic_group_id.get(g.id, frozenset())) for g in groups
        ]

        device_ids = [d.id for d in devices]
        static_keys_by_device = await load_static_group_keys_by_device_id(db, device_ids)

        exported = [_exported_device(d, sorted(static_keys_by_device.get(d.id, frozenset()))) for d in devices]
        return ExportBundle(
            schema_version=SCHEMA_VERSION,
            exported_at=now_utc(),
            source_instance=None,
            groups=exported_groups,
            devices=exported,
        )


def _exported_group(group: DeviceGroup, member_of_keys: frozenset[str]) -> ExportedDeviceGroup:
    """Build the public group definition from the stored native JSON plus the relation.

    ``group.filters`` never carries ``member_of`` from the member-of-FK phase on, but
    a stray legacy key is dropped defensively rather than echoed back — the relation
    is the only source of truth for references.
    """
    filters = None
    if group.group_type == GroupType.dynamic:
        from app.devices.schemas.filters import DeviceGroupFilters  # noqa: PLC0415

        native = dict(group.filters or {})
        native.pop("member_of", None)
        keys = sorted(member_of_keys)
        if keys:
            native["member_of"] = keys
        if native:
            filters = DeviceGroupFilters.model_validate(native)
    return ExportedDeviceGroup(
        key=group.key,
        name=group.name,
        description=group.description,
        group_type=group.group_type,
        filters=filters,
    )


def _exported_device(d: Device, static_group_keys: list[str]) -> ExportedDevice:
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
        static_groups=static_group_keys,
        device_config=dict(d.device_config or {}),
        test_data=dict(d.test_data or {}),
        original_host=OriginalHost(hostname=host.hostname, host_id=host.id),
    )
