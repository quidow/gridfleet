from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from app.models.device_event import DeviceEvent, DeviceEventType
from tests.helpers import create_device

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.host import Host


@pytest.mark.db
@pytest.mark.asyncio
async def test_device_event_id_uses_database_uuidv7_default(db_session: AsyncSession, db_host: Host) -> None:
    device = await create_device(db_session, host_id=db_host.id, name="uuidv7 device")
    event = DeviceEvent(device_id=device.id, event_type=DeviceEventType.health_check_fail, details={})
    db_session.add(event)
    await db_session.flush()

    assert isinstance(event.id, uuid.UUID)
    assert event.id.version == 7


@pytest.mark.db
@pytest.mark.asyncio
async def test_system_event_event_id_uses_database_uuidv7_default(db_session: AsyncSession) -> None:
    await db_session.execute(text("INSERT INTO system_events (type, data) VALUES ('test.event', '{}'::jsonb)"))
    result = await db_session.execute(text("SELECT event_id FROM system_events WHERE type = 'test.event'"))

    assert uuid.UUID(result.scalar_one()).version == 7
