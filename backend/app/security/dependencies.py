from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyCookie, HTTPBasic, HTTPBasicCredentials

from app.services import auth

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
