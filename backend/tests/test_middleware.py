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


@pytest.mark.asyncio
async def test_docs_oauth2_redirect_subpath_is_gated() -> None:
    """FastAPI auto-mounts /docs/oauth2-redirect. Exact-match gating would leave it open."""
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/docs/oauth2-redirect")
    # The subpath must be gated even though it is not in the literal prefix list.
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unrelated_subpath_with_gated_prefix_substring_is_not_gated() -> None:
    """A path like /docsnext that merely begins with the same string as /docs is NOT a sub-route."""
    app = _build_app()

    @app.get("/docsnext")
    async def docsnext() -> dict[str, str]:
        return {"ok": "true"}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/docsnext")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_static_path_401_emits_json_error_envelope() -> None:
    """The 401 must go through the standard error envelope and carry x-request-id
    (populated by RequestContextMiddleware running outside StaticPathsAuthMiddleware)."""
    from app.middleware import RequestContextMiddleware

    app = FastAPI(title="x", openapi_url="/openapi.json", docs_url="/docs", redoc_url="/redoc")
    # Order: add StaticPaths first (becomes inner), then RequestContext (becomes outer).
    app.add_middleware(StaticPathsAuthMiddleware)
    app.add_middleware(RequestContextMiddleware)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        r = await client.get("/docs")
    assert r.status_code == 401
    assert r.headers.get("content-type", "").startswith("application/json")
    assert r.headers.get("x-request-id")
    body = r.json()
    assert body["error"]["code"] == "UNAUTHORIZED"
    assert body["error"]["request_id"] == r.headers["x-request-id"]
