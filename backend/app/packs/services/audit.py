from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from app.events.models import SystemEvent

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
