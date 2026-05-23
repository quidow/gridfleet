import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.host.capabilities import (
    _snapshot_is_stale,
    capabilities_refresh_loop,
    clear_capabilities_snapshot,
    refresh_capabilities_snapshot,
)


def test_snapshot_staleness() -> None:
    clear_capabilities_snapshot()
    assert _snapshot_is_stale() is True


async def test_refresh_and_loop_cover_remaining_paths() -> None:
    snapshot = {"platforms": ["roku"], "tools": {"appium": "3.0.0"}, "missing_prerequisites": []}
    with patch("agent_app.host.capabilities.detect_capabilities", new_callable=AsyncMock, return_value=snapshot):
        assert await refresh_capabilities_snapshot() == snapshot

    sleep_calls = 0

    async def _sleep(_delay: float) -> None:
        nonlocal sleep_calls
        sleep_calls += 1
        raise asyncio.CancelledError

    with (
        patch(
            "agent_app.host.capabilities.refresh_capabilities_snapshot",
            new_callable=AsyncMock,
            side_effect=RuntimeError,
        ),
        patch("agent_app.host.capabilities.asyncio.sleep", side_effect=_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await capabilities_refresh_loop(interval_sec=1, refresh_immediately=True)
