import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.host.capabilities import CapabilitiesCache


def test_snapshot_staleness() -> None:
    cache = CapabilitiesCache(adapter_registry=None)
    assert cache._is_stale() is True


async def test_refresh_and_loop_cover_remaining_paths() -> None:
    cache = CapabilitiesCache(adapter_registry=None)
    snapshot = {"platforms": ["roku"], "tools": {"appium": "3.0.0"}, "missing_prerequisites": []}
    with patch.object(cache, "detect", new_callable=AsyncMock, return_value=snapshot):
        assert await cache.refresh() == snapshot

    async def _sleep(_delay: float) -> None:
        raise asyncio.CancelledError

    with (
        patch.object(cache, "refresh", new_callable=AsyncMock, side_effect=RuntimeError),
        patch("agent_app.host.capabilities.asyncio.sleep", side_effect=_sleep),
        pytest.raises(asyncio.CancelledError),
    ):
        await cache.run_refresh_loop(interval_sec=1, refresh_immediately=True)
