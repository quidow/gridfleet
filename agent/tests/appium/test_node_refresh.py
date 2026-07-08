from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from httpx2 import ASGITransport, AsyncClient

from agent_app.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http_client:
        yield http_client


@pytest.mark.asyncio
async def test_node_refresh_is_accepted_as_noop_when_loop_is_disabled(client: AsyncClient) -> None:
    app.state.node_state_loop = None

    response = await client.post("/agent/appium-nodes/refresh")

    assert response.status_code == 202


@pytest.mark.asyncio
async def test_node_refresh_wakes_constructed_loop(client: AsyncClient) -> None:
    loop = Mock()
    app.state.node_state_loop = loop
    try:
        response = await client.post("/agent/appium-nodes/refresh")
    finally:
        app.state.node_state_loop = None

    assert response.status_code == 202
    loop.wake.assert_called_once_with()
