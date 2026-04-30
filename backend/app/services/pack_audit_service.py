from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.models.system_event import SystemEvent

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


async def record_pack_upload(
    session: AsyncSession,
    *,
    username: str,
    pack_id: str,
    release: str,
    artifact_sha256: str,
    origin_filename: str,
) -> None:
    event = SystemEvent(
        event_id=str(uuid.uuid4()),
        type="driver_pack.upload",
        data={
            "uploaded_by": username,
            "pack_id": pack_id,
            "release": release,
            "artifact_sha256": artifact_sha256,
            "origin_filename": origin_filename,
        },
    )
    session.add(event)


async def record_pack_tarball_fetched(
    session: AsyncSession,
    *,
    host_id: str,
    pack_id: str,
    release: str,
    artifact_sha256: str,
) -> None:
    event = SystemEvent(
        event_id=str(uuid.uuid4()),
        type="driver_pack.tarball_fetched",
        data={
            "host_id": host_id,
            "pack_id": pack_id,
            "release": release,
            "artifact_sha256": artifact_sha256,
        },
    )
    session.add(event)
