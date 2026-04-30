from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.device import Device
from app.services.device_identity import (
    host_scoped_clause,
    is_host_scoped_identity,
    non_host_scoped_clause,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from sqlalchemy.ext.asyncio import AsyncSession


class DeviceIdentityConflictError(Exception):
    pass


async def find_device_identity_conflict(
    db: AsyncSession,
    *,
    identity_scope: str | None,
    identity_scheme: str | None,
    identity_value: str | None,
    host_id: uuid.UUID | str | None,
    exclude_device_id: uuid.UUID | None = None,
) -> Device | None:
    if not identity_value or identity_scheme is None:
        return None

    resolved_host_id = (
        uuid.UUID(str(host_id)) if host_id is not None and not isinstance(host_id, uuid.UUID) else host_id
    )

    stmt = (
        select(Device)
        .options(selectinload(Device.host))
        .where(
            Device.identity_scheme == identity_scheme,
            Device.identity_value == identity_value,
        )
    )
    if exclude_device_id is not None:
        stmt = stmt.where(Device.id != exclude_device_id)

    if is_host_scoped_identity(identity_scope=identity_scope):
        if resolved_host_id is None:
            return None
        stmt = stmt.where(Device.host_id == resolved_host_id, host_scoped_clause(Device))
    else:
        stmt = stmt.where(non_host_scoped_clause(Device))

    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


def build_device_identity_conflict_detail(
    *,
    conflict: Device,
    identity_scope: str | None,
    identity_value: str | None,
    connection_target: str | None,
) -> str:
    if is_host_scoped_identity(identity_scope=identity_scope):
        identity_label = connection_target or identity_value
        host_label = conflict.host.hostname if conflict.host is not None else str(conflict.host_id)
        return f"Host {host_label!r} already registered device {identity_label!r} (host-scoped identity)"

    host_label = conflict.host.hostname if conflict.host is not None else str(conflict.host_id)
    return f"Identity {conflict.identity_value!r} is already registered on host {host_label!r}"


async def ensure_device_identity_available(
    db: AsyncSession,
    *,
    identity_scope: str | None,
    identity_scheme: str | None,
    identity_value: str | None,
    connection_target: str | None,
    host_id: uuid.UUID | str | None,
    exclude_device_id: uuid.UUID | None = None,
) -> None:
    conflict = await find_device_identity_conflict(
        db,
        identity_scope=identity_scope,
        identity_scheme=identity_scheme,
        identity_value=identity_value,
        host_id=host_id,
        exclude_device_id=exclude_device_id,
    )
    if conflict is None:
        return
    raise DeviceIdentityConflictError(
        build_device_identity_conflict_detail(
            conflict=conflict,
            identity_scope=identity_scope,
            identity_value=identity_value,
            connection_target=connection_target,
        )
    )


async def ensure_device_payload_identity_available(
    db: AsyncSession,
    payload: Mapping[str, Any],
    *,
    exclude_device_id: uuid.UUID | None = None,
) -> None:
    await ensure_device_identity_available(
        db,
        identity_scope=payload.get("identity_scope"),
        identity_scheme=payload.get("identity_scheme"),
        identity_value=payload.get("identity_value"),
        connection_target=payload.get("connection_target"),
        host_id=payload.get("host_id"),
        exclude_device_id=exclude_device_id,
    )
