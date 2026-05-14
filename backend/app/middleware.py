from __future__ import annotations

import asyncio
from time import perf_counter
from typing import TYPE_CHECKING

from starlette.datastructures import Headers, MutableHeaders

# ``StaticPathsAuthMiddleware`` moved to ``app/auth/middleware.py`` in
# Phase 1 of the backend domain-layout refactor. Re-exported here so
# ``app/main.py`` and any other existing importer keeps resolving.
from app.auth.middleware import StaticPathsAuthMiddleware
from app.config import settings
from app.errors import error_response, request_id_from_scope
from app.metrics import record_http_request
from app.observability import (
    REQUEST_ID_HEADER,
    bind_request_context,
    clear_request_context,
    generate_request_id,
)
from app.shutdown import shutdown_coordinator

if TYPE_CHECKING:
    from collections.abc import Mapping

    from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["RequestContextMiddleware", "StaticPathsAuthMiddleware"]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self._request_timeout_sec = float(settings.request_timeout_sec)

    @staticmethod
    def _is_health_path(path: str) -> bool:
        return path in {"/health/live", "/health/ready", "/api/health"}

    @classmethod
    def _is_timeout_exempt(cls, path: str) -> bool:
        return path == "/api/events"

    @staticmethod
    def _scope_str(scope: Mapping[str, object], key: str, default: str) -> str:
        value = scope.get(key, default)
        return value if isinstance(value, str) and value else default

    @classmethod
    def _route_path(cls, scope: Mapping[str, object], fallback: str) -> str:
        route = scope.get("route")
        route_path = getattr(route, "path", fallback)
        return route_path if isinstance(route_path, str) and route_path else fallback

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = headers.get(REQUEST_ID_HEADER) or generate_request_id()
        method = self._scope_str(scope, "method", "GET")
        path = self._scope_str(scope, "path", "")
        bind_request_context(request_id=request_id, method=method, path=path)
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        scope["state"]["auth_mode"] = "disabled"
        scope["state"]["auth_username"] = None

        started = perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = int(message["status"])
                mutable_headers = MutableHeaders(raw=message.setdefault("headers", []))
                mutable_headers[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                route_path = self._route_path(scope, path)
                record_http_request(
                    method=method,
                    path=route_path,
                    status_code=status_code,
                    duration_seconds=perf_counter() - started,
                )
                clear_request_context()
            await send(message)

        if shutdown_coordinator.is_shutting_down() and not self._is_health_path(path):
            response = error_response(
                status_code=503,
                code="SHUTTING_DOWN",
                message="The backend is shutting down and not accepting new requests",
                request_id=request_id,
            )
            await response(scope, receive, send_wrapper)
            return

        shutdown_coordinator.request_started()
        try:
            if self._is_timeout_exempt(path):
                await self.app(scope, receive, send_wrapper)
            else:
                await asyncio.wait_for(self.app(scope, receive, send_wrapper), timeout=self._request_timeout_sec)
        except TimeoutError:
            if not response_started:
                response = error_response(
                    status_code=504,
                    code="REQUEST_TIMEOUT",
                    message="The request exceeded the maximum execution time",
                    request_id=request_id_from_scope(scope),
                )
                await response(scope, receive, send_wrapper)
                return
            clear_request_context()
            raise
        except Exception:
            route_path = self._route_path(scope, path)
            record_http_request(
                method=method,
                path=route_path,
                status_code=500,
                duration_seconds=perf_counter() - started,
            )
            clear_request_context()
            raise
        finally:
            shutdown_coordinator.request_finished()
