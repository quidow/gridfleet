from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

import agent_app.config as _config


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.agent_settings, "api_auth_username", None)
    monkeypatch.setattr(_config.agent_settings, "api_auth_password", None)


async def _client() -> AsyncClient:
    from agent_app.main import app

    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_health_open_when_auth_unset() -> None:
    async with await _client() as c:
        resp = await c.get("/agent/health")
    assert resp.status_code == 200


async def test_health_requires_credentials_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_config.agent_settings, "api_auth_username", "ops")
    monkeypatch.setattr(_config.agent_settings, "api_auth_password", "secret")

    async with await _client() as c:
        unauth = await c.get("/agent/health")
        assert unauth.status_code == 401
        assert "WWW-Authenticate" in unauth.headers

        wrong = await c.get(
            "/agent/health",
            headers={"Authorization": "Basic " + base64.b64encode(b"ops:wrong").decode()},
        )
        assert wrong.status_code == 401

        good = await c.get(
            "/agent/health",
            headers={"Authorization": "Basic " + base64.b64encode(b"ops:secret").decode()},
        )
        assert good.status_code == 200


async def test_non_agent_path_unaffected() -> None:
    async with await _client() as c:
        # Non-/agent path. App currently has no such route, so 404 is fine.
        resp = await c.get("/healthz")
    assert resp.status_code in (404, 405)
