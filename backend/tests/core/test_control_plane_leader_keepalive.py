"""Leader keepalive loop behavior."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.leader.advisory import LeadershipLost
from app.core.leader.keepalive import run_keepalive_once


def _mock_settings(**kwargs: object) -> MagicMock:
    defaults: dict[str, object] = {
        "general.leader_keepalive_enabled": True,
        "general.leader_keepalive_interval_sec": 5,
        "general.leader_stale_threshold_sec": 30,
    }
    defaults.update(kwargs)
    mock = MagicMock()
    mock.get = lambda key: defaults[key]  # type: ignore[return-value]
    return mock


@pytest.mark.asyncio
async def test_run_once_skips_when_disabled() -> None:
    write_called = AsyncMock()
    with patch("app.core.leader.keepalive.control_plane_leader.write_heartbeat", write_called):
        await run_keepalive_once(settings=_mock_settings(**{"general.leader_keepalive_enabled": False}))
    write_called.assert_not_called()


@pytest.mark.asyncio
async def test_run_once_propagates_leadership_lost() -> None:
    with (
        patch(
            "app.core.leader.keepalive.control_plane_leader.write_heartbeat",
            side_effect=LeadershipLost("test"),
        ),
        pytest.raises(LeadershipLost),
    ):
        await run_keepalive_once(settings=_mock_settings())


@pytest.mark.asyncio
async def test_run_once_swallows_transient_db_error() -> None:
    with patch(
        "app.core.leader.keepalive.control_plane_leader.write_heartbeat",
        side_effect=RuntimeError("transient"),
    ):
        await run_keepalive_once(settings=_mock_settings())
