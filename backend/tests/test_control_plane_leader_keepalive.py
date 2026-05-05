"""Leader keepalive loop behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.services.control_plane_leader import LeadershipLost
from app.services.control_plane_leader_keepalive import run_keepalive_once


@pytest.mark.asyncio
async def test_run_once_skips_when_disabled() -> None:
    write_called = AsyncMock()
    with (
        patch("app.services.control_plane_leader_keepalive.settings_service.get", return_value=False),
        patch("app.services.control_plane_leader_keepalive.control_plane_leader.write_heartbeat", write_called),
    ):
        await run_keepalive_once()
    write_called.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_propagates_leadership_lost() -> None:
    with (
        patch("app.services.control_plane_leader_keepalive.settings_service.get", return_value=True),
        patch(
            "app.services.control_plane_leader_keepalive.control_plane_leader.write_heartbeat",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await run_keepalive_once()


@pytest.mark.asyncio
async def test_run_once_swallows_transient_db_error() -> None:
    with (
        patch("app.services.control_plane_leader_keepalive.settings_service.get", return_value=True),
        patch(
            "app.services.control_plane_leader_keepalive.control_plane_leader.write_heartbeat",
            side_effect=RuntimeError("transient"),
        ),
    ):
        await run_keepalive_once()
