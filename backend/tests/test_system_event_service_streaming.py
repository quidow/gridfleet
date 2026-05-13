from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.models.system_event import SystemEvent
from app.services.system_event_service import iter_system_events

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_iter_system_events_streams_in_created_order(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            SystemEvent(type="test.one", data={"n": 1}),
            SystemEvent(type="test.two", data={"n": 2}),
        ]
    )
    await db_session.commit()

    seen: list[str] = []
    async for event in iter_system_events(db_session, batch_size=1):
        if event.type.startswith("test."):
            seen.append(event.type)

    assert seen == ["test.one", "test.two"]
