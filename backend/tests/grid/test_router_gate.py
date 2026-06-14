from __future__ import annotations

import base64

import pytest
from httpx import ASGITransport, AsyncClient

from app.auth import auth_settings as settings
from app.main import app

OPEN_PATH_PREFIXES = (
    "/health/live",
    "/health/ready",
    "/api/health",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/session",
    # /metrics, /docs, /redoc, /openapi.json are protected by StaticPathsAuthMiddleware,
    # not by require_any_auth — they are not APIRoute instances so the audit skips them,
    # but /metrics IS an APIRoute; add it here so the inverted walk doesn't false-positive.
    "/metrics",
)


@pytest.fixture(autouse=True)
def _auth_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "auth_enabled", True, raising=False)
    monkeypatch.setattr(settings, "auth_username", "alice", raising=False)
    monkeypatch.setattr(settings, "auth_password", "pw", raising=False)
    monkeypatch.setattr(settings, "auth_session_secret", "secret-padded-to-32-bytes-test-only", raising=False)
    monkeypatch.setattr(settings, "machine_auth_username", "bot", raising=False)
    monkeypatch.setattr(settings, "machine_auth_password", "shh", raising=False)


def _basic_header() -> dict[str, str]:
    return {"authorization": "Basic " + base64.b64encode(b"bot:shh").decode()}


@pytest.mark.asyncio
async def test_open_paths_reachable_without_auth() -> None:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        for path in ("/health/live", "/api/auth/session", "/api/auth/login"):
            method = "POST" if path == "/api/auth/login" else "GET"
            r = await client.request(method, path, json={} if method == "POST" else None)
            # 200, 422 (validation), or similar — anything but 401/403.
            assert r.status_code not in (401, 403), f"{path} unexpectedly gated: {r.status_code}"


@pytest.mark.asyncio
async def test_protected_path_sample_requires_auth() -> None:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        unauth = await client.get("/api/devices")
        ok = await client.get("/api/devices", headers=_basic_header())
    assert unauth.status_code == 401
    # Auth passed: any status other than 401 (handler may fail with 500 if DB/settings
    # aren't initialised in the lightweight test context — that's fine, auth did its job).
    assert ok.status_code != 401


@pytest.mark.asyncio
async def test_static_paths_require_auth() -> None:
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/metrics"):
            r = await client.get(path)
            assert r.status_code == 401, path


def _collect_dep_names(d: object, names: set[str]) -> None:
    call = getattr(d, "call", None)
    if call is not None:
        names.add(getattr(call, "__name__", ""))
    for sub in getattr(d, "dependencies", ()):
        _collect_dep_names(sub, names)


def test_all_protected_routes_have_require_any_auth() -> None:
    """Inverted audit: every HTTP route must either be open-listed or run require_any_auth."""
    from fastapi.routing import APIRoute

    from app.auth.dependencies import require_any_auth

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if any(path == open_ or path.startswith(open_ + "/") for open_ in OPEN_PATH_PREFIXES):
            continue
        # Each remaining route should resolve require_any_auth somewhere in its dependant chain.
        names: set[str] = set()
        _collect_dep_names(route.dependant, names)
        assert require_any_auth.__name__ in names, f"Route {path} is not gated by require_any_auth"
