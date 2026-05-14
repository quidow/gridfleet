from __future__ import annotations

import base64
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import service as auth
from app.config import settings
from app.security.dependencies import require_any_auth


def _app_with_require() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    async def probe_get(result: Annotated[auth.RequestAuthResult, Depends(require_any_auth)]) -> dict[str, str | None]:
        return {"mode": result.mode, "username": result.username}

    @app.post("/probe")
    async def probe_post(result: Annotated[auth.RequestAuthResult, Depends(require_any_auth)]) -> dict[str, str | None]:
        return {"mode": result.mode, "username": result.username}

    return app


def _basic(username: str, password: str) -> str:
    raw = f"{username}:{password}".encode()
    return "Basic " + base64.b64encode(raw).decode()


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_password", "pw", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "secret", raising=False)
    monkeypatch.setattr(settings, "auth_session_ttl_sec", 60, raising=False)
    monkeypatch.setattr(settings, "machine_auth_username", "bot", raising=False)
    monkeypatch.setattr(settings, "machine_auth_password", "shh", raising=False)


@pytest.mark.asyncio
async def test_require_any_auth_disabled_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", False, raising=False)
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/probe")
    assert r.status_code == 200
    assert r.json() == {"mode": "disabled", "username": None}


@pytest.mark.asyncio
async def test_require_any_auth_machine_basic_succeeds() -> None:
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/probe", headers={"authorization": _basic("bot", "shh")})
    assert r.status_code == 200
    assert r.json()["mode"] == "machine"
    assert r.json()["username"] == "bot"


@pytest.mark.asyncio
async def test_require_any_auth_machine_wrong_password_falls_through_to_401() -> None:
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/probe", headers={"authorization": _basic("bot", "wrong")})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_require_any_auth_browser_get_succeeds() -> None:
    token, _ = auth.issue_session()
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get(
            "/probe",
            headers={"cookie": f"{auth.SESSION_COOKIE_NAME}={token}"},
        )
    assert r.status_code == 200
    assert r.json()["mode"] == "browser"
    assert r.json()["username"] == "alice"


@pytest.mark.asyncio
async def test_require_any_auth_browser_post_requires_csrf() -> None:
    token, session = auth.issue_session()
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        without = await client.post(
            "/probe",
            headers={"cookie": f"{auth.SESSION_COOKIE_NAME}={token}"},
        )
        with_csrf = await client.post(
            "/probe",
            headers={
                "cookie": f"{auth.SESSION_COOKIE_NAME}={token}",
                "x-csrf-token": session.csrf_token or "",
            },
        )
    assert without.status_code == 403
    assert with_csrf.status_code == 200


@pytest.mark.asyncio
async def test_require_any_auth_no_credentials_returns_401() -> None:
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/probe")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_require_any_auth_machine_wins_over_cookie() -> None:
    token, _ = auth.issue_session()
    app = _app_with_require()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get(
            "/probe",
            headers={
                "cookie": f"{auth.SESSION_COOKIE_NAME}={token}",
                "authorization": _basic("bot", "shh"),
            },
        )
    assert r.status_code == 200
    assert r.json()["mode"] == "machine"
