from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from app.auth import service as auth
from app.auth.schemas import AuthLoginRequest, AuthSessionRead
from app.core.error_responses import RESPONSES_401

router = APIRouter(prefix="/api/auth", tags=["auth"], responses={**RESPONSES_401})


def _session_payload(session: auth.SessionState) -> dict[str, Any]:
    return {
        "enabled": session.enabled,
        "authenticated": session.authenticated,
        "username": session.username,
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at,
    }


@router.get("/session", response_model=AuthSessionRead)
async def get_session(request: Request, response: Response) -> dict[str, Any]:
    session = auth.resolve_browser_session_from_headers(request.headers)
    if session.enabled and not session.authenticated:
        auth.clear_session_cookie(response)
    return _session_payload(session)


@router.post("/login", response_model=AuthSessionRead)
async def login(request: AuthLoginRequest, response: Response) -> dict[str, Any]:
    if not auth.is_auth_enabled():
        return {
            "enabled": False,
            "authenticated": False,
            "username": None,
            "csrf_token": None,
            "expires_at": None,
        }

    if not auth.authenticate_operator(request.username, request.password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    token, session = auth.issue_session()
    auth.set_session_cookie(response, token)
    return _session_payload(session)


@router.post("/logout", response_model=AuthSessionRead)
async def logout(response: Response) -> dict[str, Any]:
    if auth.is_auth_enabled():
        auth.clear_session_cookie(response)
        return {
            "enabled": True,
            "authenticated": False,
            "username": None,
            "csrf_token": None,
            "expires_at": None,
        }

    return {
        "enabled": False,
        "authenticated": False,
        "username": None,
        "csrf_token": None,
        "expires_at": None,
    }
