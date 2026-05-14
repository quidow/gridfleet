"""Live-route contract tests: validate that real route output satisfies its response schema.

These tests spin up the ASGI app via ``httpx.ASGITransport`` so any future
divergence between a schema declaration and the actual route payload is caught
before PR 2 wires ``response_model=`` on those routes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.appium import appium_mgr
from agent_app.host.schemas import HealthResponse
from agent_app.main import app

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def health_client() -> AsyncGenerator[AsyncClient, None]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://agent-test") as client:
        yield client
    await appium_mgr.shutdown()


async def test_health_response_matches_live_route_output(health_client: AsyncClient) -> None:
    with patch(
        "agent_app.host.router.get_capabilities_snapshot",
        return_value={"platforms": [], "tools": {}, "missing_prerequisites": []},
    ):
        resp = await health_client.get("/agent/health")
    assert resp.status_code == 200
    HealthResponse.model_validate(resp.json())
