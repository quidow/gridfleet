from __future__ import annotations

import base64

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.middleware import StaticPathsAuthMiddleware


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_password", "pw", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "machine_auth_username", "bot", raising=False)
    monkeypatch.setattr(settings, "machine_auth_password", "shh", raising=False)


def _build_app() -> FastAPI:
    app = FastAPI(title="x", openapi_url="/openapi.json", docs_url="/docs", redoc_url="/redoc")
    app.add_middleware(StaticPathsAuthMiddleware)
    return app


@pytest.mark.asyncio
async def test_static_paths_require_auth() -> None:
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            r = await client.get(path)
            assert r.status_code == 401, path


@pytest.mark.asyncio
async def test_static_paths_allow_machine_basic() -> None:
    app = _build_app()
    creds = "Basic " + base64.b64encode(b"bot:shh").decode()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/openapi.json", headers={"authorization": creds})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_static_paths_disabled_auth_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False, raising=False)
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/openapi.json")
    assert r.status_code == 200
