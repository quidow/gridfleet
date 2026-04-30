from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from adapter.lifecycle import lifecycle_action


class _Ctx:
    host_id = "h1"
    device_identity_value = "SIM123"


@pytest.mark.asyncio
@patch("adapter.lifecycle.run_cmd", new_callable=AsyncMock, return_value="")
async def test_boot_simulator(mock_cmd: AsyncMock) -> None:
    result = await lifecycle_action("boot", {}, _Ctx())
    assert result.ok is True
    assert result.state == "booted"


@pytest.mark.asyncio
@patch("adapter.lifecycle.run_cmd", new_callable=AsyncMock, return_value="")
async def test_shutdown_simulator(mock_cmd: AsyncMock) -> None:
    result = await lifecycle_action("shutdown", {}, _Ctx())
    assert result.ok is True
    assert result.state == "shutdown"
