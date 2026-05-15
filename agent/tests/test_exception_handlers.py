"""Verify global exception handlers emit ErrorEnvelope-shaped responses."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from agent_app.error_codes import AgentErrorCode
from agent_app.main import app
from agent_app.observability import REQUEST_ID_HEADER


@pytest.fixture
async def client() -> AsyncClient:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_validation_error_returns_envelope(client: AsyncClient) -> None:
    resp = await client.post("/agent/appium/start", json={}, headers={REQUEST_ID_HEADER: "req-123"})

    assert resp.status_code == 422
    assert resp.headers[REQUEST_ID_HEADER] == "req-123"
    body = resp.json()
    assert body["detail"]["code"] == AgentErrorCode.INVALID_PAYLOAD.value
    assert body["detail"]["message"] == "Request validation failed"
    assert isinstance(body["detail"]["errors"], list)
    assert body["detail"]["errors"], "errors list must be non-empty"


async def test_unhandled_exception_returns_envelope(client: AsyncClient) -> None:
    from agent_app.appium.dependencies import get_appium_mgr

    def _boom() -> None:
        raise ValueError("synthetic")

    app.dependency_overrides[get_appium_mgr] = _boom
    try:
        resp = await client.get("/agent/health")
    finally:
        app.dependency_overrides.pop(get_appium_mgr, None)

    assert resp.status_code == 500
    body = resp.json()
    assert body["detail"]["code"] == AgentErrorCode.INTERNAL_ERROR.value
    assert body["detail"]["message"] == "Internal server error"


async def test_http_exception_passthrough_unchanged(client: AsyncClient) -> None:
    resp = await client.post(
        "/agent/appium/stop",
        json={"port": 1},
    )
    assert resp.status_code == 422
