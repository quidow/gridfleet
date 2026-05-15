from __future__ import annotations

import base64
import binascii
import hmac
from typing import TYPE_CHECKING

from pydantic import SecretStr

from agent_app.config import agent_settings

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Receive, Scope, Send


_PROTECTED_PREFIX = "/agent/"


class BasicAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        username = agent_settings.api_auth.api_auth_username
        password = _secret_value(agent_settings.api_auth.api_auth_password)
        if not username or not password:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if not isinstance(path, str) or not path.startswith(_PROTECTED_PREFIX):
            await self.app(scope, receive, send)
            return

        if _credentials_match(scope, username, password):
            await self.app(scope, receive, send)
            return

        await _send_unauthorized(send)


def _secret_value(value: SecretStr | str | None) -> str | None:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return value


def _credentials_match(scope: Scope, expected_username: str, expected_password: str) -> bool:
    headers = dict(scope.get("headers") or ())
    raw = headers.get(b"authorization")
    if not raw:
        return False
    try:
        decoded_header = raw.decode("latin-1")
    except UnicodeDecodeError:
        return False
    scheme, _, encoded = decoded_header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded).decode("latin-1")
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    username, separator, password = decoded.partition(":")
    if not separator:
        return False
    try:
        username_bytes = username.encode("latin-1")
        password_bytes = password.encode("latin-1")
        expected_username_bytes = expected_username.encode("latin-1")
        expected_password_bytes = expected_password.encode("latin-1")
    except UnicodeEncodeError:
        return False
    user_ok = hmac.compare_digest(username_bytes, expected_username_bytes)
    pass_ok = hmac.compare_digest(password_bytes, expected_password_bytes)
    return user_ok and pass_ok


async def _send_unauthorized(send: Send) -> None:
    body = b'{"detail":"unauthorized"}'
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"www-authenticate", b'Basic realm="gridfleet-agent"'),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body, "more_body": False})
