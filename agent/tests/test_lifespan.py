from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from agent_app.host.capabilities import CapabilitiesCache
from agent_app.lifespan import lifespan
from agent_app.main import app
from agent_app.registration import RegistrationService

pytestmark = pytest.mark.asyncio


async def test_lifespan_starts_appium_log_maintenance() -> None:
    stop_event = asyncio.Event()

    async def _wait_forever(*_args: object, **_kwargs: object) -> None:
        await stop_event.wait()

    try:
        with (
            patch.object(CapabilitiesCache, "refresh", new_callable=AsyncMock),
            patch.object(CapabilitiesCache, "run_refresh_loop", side_effect=_wait_forever),
            patch.object(RegistrationService, "run", side_effect=_wait_forever),
            patch("agent_app.appium.appium_mgr.start_log_maintenance") as start_maintenance,
            patch("agent_app.appium.appium_mgr.shutdown", new_callable=AsyncMock),
        ):
            async with lifespan(app):
                pass
    finally:
        stop_event.set()

    start_maintenance.assert_called_once_with()
