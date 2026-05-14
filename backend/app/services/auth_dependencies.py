from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.services import auth as auth_module


async def require_admin(request: Request) -> str:
    """Return the authenticated admin username; 403 if anonymous and auth is enforced.

    Authentication is enforced upstream by `require_any_auth` (FastAPI dependency on
    every protected router include). That dependency mirrors the resolved username
    into ``request.state.auth_username``; this dependency re-checks it so an
    admin-only route fails closed if the upstream dependency was skipped.
    """
    if not auth_module.is_auth_enabled():
        return "anonymous-admin"
    # `require_any_auth` writes auth_username into scope["state"] (a plain dict).
    # FastAPI's `request.state` is a `State` object backed by the same dict via
    # `scope["state"]`, so both access patterns are equivalent at runtime.
    username: str | None = None
    scope_state = request.scope.get("state")
    if isinstance(scope_state, dict):
        raw = scope_state.get("auth_username")
        username = raw if isinstance(raw, str) and raw else None
    if not username:
        # Fallback: check request.state (Starlette State wraps scope["state"]).
        username = getattr(request.state, "auth_username", None)
        if not isinstance(username, str) or not username:
            username = None
    if not username:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return username
