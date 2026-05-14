"""Auth-domain middleware.

``StaticPathsAuthMiddleware`` gates non-API surfaces (``/docs``,
``/redoc``, ``/metrics``, ``/openapi.json``) when auth is enabled. The
API routes themselves are gated by ``require_any_auth`` injected at the
router level, not via middleware.

Phase 1 of the backend domain-layout refactor extracted this class out
of ``app/middleware.py``. The old location keeps a thin re-export shim
so existing ``main.py`` imports keep resolving.
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware

from app.auth import service as _auth_service
from app.errors import error_response, request_id_from_request

if TYPE_CHECKING:
    from starlette.middleware.base import RequestResponseEndpoint
    from starlette.requests import Request
    from starlette.responses import Response


class StaticPathsAuthMiddleware(BaseHTTPMiddleware):
    # Prefix match so sub-routes (e.g. /docs/oauth2-redirect) are gated
    # alongside their parent. FastAPI auto-adds /docs/oauth2-redirect; an
    # exact-match list would leave it public.
    GATED_PREFIXES: tuple[str, ...] = ("/docs", "/redoc", "/metrics", "/openapi.json")

    @classmethod
    def _is_gated(cls, path: str) -> bool:
        return any(path == prefix or path.startswith(prefix + "/") for prefix in cls.GATED_PREFIXES)

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not self._is_gated(request.url.path) or not _auth_service.is_auth_enabled():
            return await call_next(request)

        authorization = request.headers.get("authorization")
        if authorization:
            scheme, _, encoded = authorization.partition(" ")
            if scheme.lower() == "basic" and encoded:
                try:
                    decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                except (ValueError, UnicodeDecodeError, binascii.Error):
                    decoded = ""
                user, sep, password = decoded.partition(":")
                if sep and _auth_service.check_machine_credentials(user, password):
                    return await call_next(request)

        token = request.cookies.get(_auth_service.SESSION_COOKIE_NAME)
        if token and _auth_service.resolve_browser_session_from_token(token).authenticated:
            return await call_next(request)

        # Route through the standard JSON error envelope so the response
        # carries x-request-id (populated by RequestContextMiddleware
        # running outside us) and matches the rest of the API's error shape.
        return error_response(
            status_code=401,
            code="UNAUTHORIZED",
            message="Authentication is required",
            request_id=request_id_from_request(request),
        )
