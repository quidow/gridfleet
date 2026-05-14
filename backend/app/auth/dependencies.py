"""Auth domain dependencies.

Merges the former ``app/services/auth_dependencies.py`` (admin gating)
with ``app/security/dependencies.py`` (machine + browser auth wiring).
``AdminDep`` moved from ``app/dependencies.py`` to live next to
``require_admin``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie, HTTPBasic, HTTPBasicCredentials

from app.auth import service as auth

_cookie_scheme = APIKeyCookie(name=auth.SESSION_COOKIE_NAME, auto_error=False)
_basic_scheme = HTTPBasic(auto_error=False)


def _machine_auth(
    creds: Annotated[HTTPBasicCredentials | None, Depends(_basic_scheme)],
) -> str | None:
    if creds is None or not auth.is_auth_enabled():
        return None
    return auth.check_machine_credentials(creds.username, creds.password)


def _browser_session(
    token: Annotated[str | None, Depends(_cookie_scheme)],
) -> auth.SessionState:
    if not auth.is_auth_enabled():
        return auth.SessionState(False, False, None, None, None)
    return auth.resolve_browser_session_from_token(token)


def _attach_and_return(request: Request, result: auth.RequestAuthResult) -> auth.RequestAuthResult:
    request.state.auth_mode = result.mode
    request.state.auth_username = result.username
    return result


def require_any_auth(
    request: Request,
    machine: Annotated[str | None, Depends(_machine_auth)],
    session: Annotated[auth.SessionState, Depends(_browser_session)],
) -> auth.RequestAuthResult:
    if not auth.is_auth_enabled():
        return _attach_and_return(request, auth.RequestAuthResult(mode="disabled"))
    if machine is not None:
        return _attach_and_return(request, auth.RequestAuthResult(mode="machine", username=machine))
    if session.authenticated:
        if request.method in auth.MUTATING_METHODS and not auth.require_valid_csrf(request.headers, session.csrf_token):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token mismatch")
        result = auth.RequestAuthResult(
            mode="browser",
            username=session.username,
            csrf_token=session.csrf_token,
            expires_at=session.expires_at,
        )
        return _attach_and_return(request, result)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")


async def require_admin(request: Request) -> str:
    """Return the authenticated admin username; 403 if anonymous and auth is enforced.

    Authentication is enforced upstream by ``require_any_auth`` (FastAPI
    dependency on every protected router include). That dependency mirrors
    the resolved username into ``request.state.auth_username``; this
    dependency re-checks it so an admin-only route fails closed if the
    upstream dependency was skipped.
    """
    if not auth.is_auth_enabled():
        return "anonymous-admin"
    # ``require_any_auth`` writes auth_username into scope["state"] (a
    # plain dict). FastAPI's ``request.state`` is a ``State`` object
    # backed by the same dict via ``scope["state"]``, so both access
    # patterns are equivalent at runtime.
    username: str | None = None
    scope_state = request.scope.get("state")
    if isinstance(scope_state, dict):
        raw = scope_state.get("auth_username")
        username = raw if isinstance(raw, str) and raw else None
    if not username:
        username = getattr(request.state, "auth_username", None)
        if not isinstance(username, str) or not username:
            username = None
    if not username:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return username


AdminDep = Annotated[str, Depends(require_admin)]
