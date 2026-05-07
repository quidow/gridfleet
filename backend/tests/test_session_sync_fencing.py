"""session_sync must not create or end Session rows after losing leadership."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, select

from app.models.session import Session
from app.services.control_plane_leader import LeadershipLost
from app.services.session_sync import _sync_sessions

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.db
@pytest.mark.asyncio
async def test_sync_sessions_aborts_after_grid_call_when_leadership_lost(
    db_session: AsyncSession,
) -> None:
    fake_grid = {"value": {"ready": True, "nodes": []}}
    initial_count = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()

    with (
        patch(
            "app.services.session_sync.grid_service.get_grid_status",
            new_callable=AsyncMock,
            return_value=fake_grid,
        ),
        patch(
            "app.services.session_sync.assert_current_leader",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await _sync_sessions(db_session)

    final_count = (await db_session.execute(select(func.count()).select_from(Session))).scalar_one()
    assert final_count == initial_count
