import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.services.auth_dependencies import require_admin


@pytest.mark.asyncio
async def test_require_admin_allows_authenticated() -> None:
    app = FastAPI()

    @app.get("/protected")
    async def protected(username: str = Depends(require_admin)) -> dict[str, str]:
        return {"user": username}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # ASGI scope without state: dependency must read scope state via request
        res = await client.get("/protected", headers={"X-Auth-Username": "alice"})
    # When auth is disabled (default), require_admin returns "anonymous-admin"
    assert res.status_code == 200


@pytest.mark.asyncio
async def test_require_admin_rejects_when_auth_enabled_and_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.auth import service as auth_module

    monkeypatch.setattr(auth_module, "is_auth_enabled", lambda: True)

    app = FastAPI()

    @app.get("/protected")
    async def protected(username: str = Depends(require_admin)) -> dict[str, str]:
        return {"user": username}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/protected")
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_require_admin_returns_username_when_auth_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When auth is enabled, require_any_auth sets auth_username; require_admin reads it."""
    from app.config import settings
    from app.security.dependencies import require_any_auth

    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_username", "admin")
    monkeypatch.setattr(settings, "auth_password", "secret")
    monkeypatch.setattr(settings, "auth_session_secret", "session-secret-for-tests")
    monkeypatch.setattr(settings, "machine_auth_username", "machine")
    monkeypatch.setattr(settings, "machine_auth_password", "machine-secret")

    # Route has both require_any_auth (sets request.state.auth_username) and
    # require_admin (reads request.state.auth_username).
    app = FastAPI()

    @app.get("/api/protected", dependencies=[Depends(require_any_auth)])
    async def protected(username: str = Depends(require_admin)) -> dict[str, str]:
        return {"user": username}

    from app.middleware import RequestContextMiddleware

    wrapped_app = RequestContextMiddleware(app)

    import base64

    token = base64.b64encode(b"machine:machine-secret").decode()

    transport = ASGITransport(app=wrapped_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get(
            "/api/protected",
            headers={"Authorization": f"Basic {token}"},
        )
    assert res.status_code == 200
    assert res.json()["user"] == "machine"
