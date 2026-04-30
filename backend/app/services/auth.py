from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from http.cookies import CookieError, SimpleCookie
from typing import TYPE_CHECKING, Any, Literal, cast

from app.config import settings

if TYPE_CHECKING:
    from starlette.datastructures import Headers
    from starlette.responses import Response

SESSION_COOKIE_NAME = "gridfleet_session"
CSRF_HEADER_NAME = "x-csrf-token"
AUTH_STATE_EXEMPT_PATHS = {
    "/api/auth/login",
    "/api/auth/session",
}
AUTH_OPEN_PATHS = {
    "/health/live",
    "/health/ready",
    "/api/health",
    "/api/driver-packs/catalog",
}
AUTH_OPEN_PREFIXES = ("/api/runs", "/api/sessions")
PROTECTED_PREFIXES = ("/api/", "/agent/", "/docs", "/redoc")
PROTECTED_EXACT_PATHS = {"/metrics", "/openapi.json"}
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class SessionState:
    enabled: bool
    authenticated: bool
    username: str | None
    csrf_token: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class RequestAuthResult:
    mode: Literal["disabled", "browser", "machine", "unauthenticated"]
    username: str | None = None
    csrf_token: str | None = None
    expires_at: datetime | None = None


def validate_process_configuration() -> None:
    if not settings.auth_enabled:
        return
    required_values = {
        "GRIDFLEET_AUTH_USERNAME": settings.auth_username,
        "GRIDFLEET_AUTH_PASSWORD": settings.auth_password,
        "GRIDFLEET_AUTH_SESSION_SECRET": settings.auth_session_secret,
        "GRIDFLEET_MACHINE_AUTH_USERNAME": settings.machine_auth_username,
        "GRIDFLEET_MACHINE_AUTH_PASSWORD": settings.machine_auth_password,
    }
    missing = [name for name, value in required_values.items() if not value]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(f"Auth is enabled but required settings are missing: {joined}")


def is_auth_enabled() -> bool:
    return bool(settings.auth_enabled)


def is_protected_path(path: str) -> bool:
    if path in AUTH_OPEN_PATHS or path in AUTH_STATE_EXEMPT_PATHS:
        return False
    if path.startswith(AUTH_OPEN_PREFIXES):
        return False
    if path in PROTECTED_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PROTECTED_PREFIXES)


def requires_csrf_check(path: str, method: str) -> bool:
    if method.upper() not in MUTATING_METHODS:
        return False
    return path not in AUTH_STATE_EXEMPT_PATHS


def operator_credentials() -> tuple[str, str]:
    username = settings.auth_username or ""
    password = settings.auth_password or ""
    return username, password


def machine_credentials() -> tuple[str, str]:
    username = settings.machine_auth_username or ""
    password = settings.machine_auth_password or ""
    return username, password


def _base64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}")


def _credential_fingerprint(username: str, password: str) -> str:
    digest = hashlib.sha256()
    digest.update(username.encode("utf-8"))
    digest.update(b":")
    digest.update(password.encode("utf-8"))
    return digest.hexdigest()


def _operator_credential_fingerprint() -> str:
    username, password = operator_credentials()
    return _credential_fingerprint(username, password)


def _json_dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _sign_payload(encoded_payload: str) -> str:
    secret = cast("str", settings.auth_session_secret)
    signature = hmac.new(secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256).digest()
    return _base64url_encode(signature)


def issue_session(username: str) -> tuple[str, SessionState]:
    now = datetime.now(UTC)
    expires_at = datetime.fromtimestamp(now.timestamp() + settings.auth_session_ttl_sec, tz=UTC)
    csrf_token = secrets.token_urlsafe(24)
    payload = {
        "sub": username,
        "csrf": csrf_token,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "fp": _operator_credential_fingerprint(),
    }
    encoded_payload = _base64url_encode(_json_dumps(payload))
    signature = _sign_payload(encoded_payload)
    token = f"v1.{encoded_payload}.{signature}"
    session = SessionState(
        enabled=True,
        authenticated=True,
        username=username,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )
    return token, session


def _decode_session_payload(token: str) -> dict[str, Any] | None:
    try:
        version, encoded_payload, signature = token.split(".", 2)
    except ValueError:
        return None
    if version != "v1":
        return None
    expected_signature = _sign_payload(encoded_payload)
    if not hmac.compare_digest(signature, expected_signature):
        return None
    try:
        payload = json.loads(_base64url_decode(encoded_payload))
    except (ValueError, json.JSONDecodeError, binascii.Error):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def resolve_browser_session_from_headers(headers: Headers) -> SessionState:
    if not is_auth_enabled():
        return SessionState(
            enabled=False,
            authenticated=False,
            username=None,
            csrf_token=None,
            expires_at=None,
        )

    token = _read_cookie(headers, SESSION_COOKIE_NAME)
    if not token:
        return SessionState(
            enabled=True,
            authenticated=False,
            username=None,
            csrf_token=None,
            expires_at=None,
        )

    payload = _decode_session_payload(token)
    if payload is None:
        return SessionState(True, False, None, None, None)

    try:
        username = payload["sub"]
        csrf_token = payload["csrf"]
        expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        fingerprint = payload["fp"]
    except (KeyError, TypeError, ValueError):
        return SessionState(True, False, None, None, None)

    if not isinstance(username, str) or not username:
        return SessionState(True, False, None, None, None)
    if not isinstance(csrf_token, str) or not csrf_token:
        return SessionState(True, False, None, None, None)
    if not isinstance(fingerprint, str) or not fingerprint:
        return SessionState(True, False, None, None, None)
    if expires_at <= datetime.now(UTC):
        return SessionState(True, False, None, None, None)
    if not hmac.compare_digest(fingerprint, _operator_credential_fingerprint()):
        return SessionState(True, False, None, None, None)

    return SessionState(
        enabled=True,
        authenticated=True,
        username=username,
        csrf_token=csrf_token,
        expires_at=expires_at,
    )


def resolve_request_auth(headers: Headers) -> RequestAuthResult:
    if not is_auth_enabled():
        return RequestAuthResult(mode="disabled")

    machine_auth = _authenticate_basic_auth(headers)
    if machine_auth is not None:
        return RequestAuthResult(mode="machine", username=machine_auth)

    browser_session = resolve_browser_session_from_headers(headers)
    if browser_session.authenticated:
        return RequestAuthResult(
            mode="browser",
            username=browser_session.username,
            csrf_token=browser_session.csrf_token,
            expires_at=browser_session.expires_at,
        )

    return RequestAuthResult(mode="unauthenticated")


def require_valid_csrf(headers: Headers, csrf_token: str | None) -> bool:
    if not csrf_token:
        return False
    provided = headers.get(CSRF_HEADER_NAME)
    if provided is None:
        return False
    return hmac.compare_digest(provided, csrf_token)


def authenticate_operator(username: str, password: str) -> bool:
    expected_username, expected_password = operator_credentials()
    return hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password)


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=settings.auth_session_ttl_sec,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


def _authenticate_basic_auth(headers: Headers) -> str | None:
    if not is_auth_enabled():
        return None
    authorization = headers.get("authorization")
    if not authorization:
        return None
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return None
    try:
        decoded = base64.b64decode(encoded).decode("utf-8")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None
    username, separator, password = decoded.partition(":")
    if not separator:
        return None
    expected_username, expected_password = machine_credentials()
    if not (hmac.compare_digest(username, expected_username) and hmac.compare_digest(password, expected_password)):
        return None
    return username


def _read_cookie(headers: Headers, name: str) -> str | None:
    raw_cookie = headers.get("cookie")
    if not raw_cookie:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(raw_cookie)
    except (CookieError, ValueError):
        return None
    morsel = cookie.get(name)
    if morsel is None:
        return None
    value = morsel.value
    return value if isinstance(value, str) and value else None
