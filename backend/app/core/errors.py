from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.core.metrics_recorders import HTTP_UNHANDLED_EXCEPTIONS_TOTAL
from app.core.observability import get_logger

logger = get_logger(__name__)


def _templated_path(request: Request) -> str:
    """Return the matched route's templated path (e.g. ``/api/sessions/{id}/finished``).

    Mirrors the HTTP-metrics middleware so the exception counter shares the same
    bounded-cardinality label instead of the raw, per-resource URL path.
    """
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return request.url.path


def _pgcode(exc: BaseException) -> str:
    """Postgres SQLSTATE for a DBAPIError, walking the driver cause chain; else ""."""
    if not isinstance(exc, DBAPIError):
        return ""
    cause: BaseException | None = exc.orig
    while cause is not None:
        sqlstate = getattr(cause, "sqlstate", None)
        if isinstance(sqlstate, str) and sqlstate:
            return sqlstate
        cause = cause.__cause__
    return ""


class AppError(Exception):
    """Base for backend-raised, response-shaping exceptions.

    Only useful for exceptions raised inside FastAPI's ExceptionMiddleware
    (route handlers, services, dependencies). Middleware-level sites must
    call ``envelope_response`` directly.
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, details: object | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class AgentCallError(AppError):
    code = "AGENT_UNREACHABLE"
    status_code = 502

    def __init__(
        self,
        host: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        transport_outcome: str | None = None,
        error_category: str | None = None,
    ) -> None:
        super().__init__(message, details=details or {"host": host})
        self.host = host
        self.transport_outcome = transport_outcome
        self.error_category = error_category

    @property
    def error_code(self) -> str:
        return self.code


class AgentUnreachableError(AgentCallError):
    code = "AGENT_UNREACHABLE"


class CircuitOpenError(AgentCallError):
    code = "CIRCUIT_OPEN"
    status_code = 503

    def __init__(self, host: str, *, retry_after_seconds: float | None = None) -> None:
        details: dict[str, Any] = {"host": host}
        if retry_after_seconds is not None:
            details["retry_after_seconds"] = round(retry_after_seconds, 3)
        super().__init__(host, f"Host {host} is temporarily unreachable", details=details)


class AgentResponseError(AgentCallError):
    code = "AGENT_RESPONSE_ERROR"

    def __init__(
        self,
        host: str,
        message: str,
        *,
        http_status: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        merged: dict[str, Any] = {"host": host}
        if http_status is not None:
            merged["http_status"] = http_status
        if details:
            merged.update(details)
        super().__init__(host, message, details=merged)
        self.http_status = http_status


class PackUnavailableError(LookupError):
    code = "pack_unavailable"

    def __init__(self, pack_id: str) -> None:
        super().__init__(pack_id)
        self.pack_id = pack_id


class PackDisabledError(LookupError):
    code = "pack_disabled"

    def __init__(self, pack_id: str) -> None:
        super().__init__(pack_id)
        self.pack_id = pack_id


class PackDrainingError(LookupError):
    code = "pack_draining"

    def __init__(self, pack_id: str) -> None:
        super().__init__(pack_id)
        self.pack_id = pack_id


class PlatformRemovedError(LookupError):
    code = "platform_removed"

    def __init__(self, pack_id: str, platform_id: str) -> None:
        super().__init__(f"{pack_id}:{platform_id}")
        self.pack_id = pack_id
        self.platform_id = platform_id


def envelope_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str | None,
    details: object | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id if isinstance(request_id, str) and request_id else None,
        }
    }
    if details is not None:
        body["error"]["details"] = jsonable_encoder(details)
    return JSONResponse(status_code=status_code, content=body, headers=headers)


def _http_error_code(status_code: int) -> str:
    if status_code == 401:
        return "UNAUTHORIZED"
    if status_code == 403:
        return "FORBIDDEN"
    if status_code == 404:
        return "NOT_FOUND"
    if status_code == 409:
        return "CONFLICT"
    if status_code in (400, 422):
        return "VALIDATION_ERROR"
    if status_code == 503:
        return "SERVICE_UNAVAILABLE"
    return "HTTP_ERROR"


def _http_error_payload(exc: HTTPException) -> tuple[str, object | None]:
    detail = exc.detail
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message:
            return message, detail
        return "Request failed", detail
    if isinstance(detail, list):
        return "Request validation failed", detail
    return "Request failed", detail


def classify_httpx_transport(exc: Exception) -> tuple[str, str]:
    """Return (transport_outcome, error_category) for an httpx exception."""
    category = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        return "timeout", category
    if isinstance(exc, httpx.ConnectError):
        message = str(exc).lower()
        dns_markers = (
            "name resolution",
            "temporary failure",
            "nodename nor servname",
            "name or service not known",
        )
        if any(marker in message for marker in dns_markers):
            return "dns_error", category
        return "connect_error", category
    return "unexpected_error", category


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return envelope_response(
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
            request_id=getattr(request.state, "request_id", None),
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return envelope_response(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request validation failed",
            request_id=getattr(request.state, "request_id", None),
            details=exc.errors(),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        message, details = _http_error_payload(exc)
        return envelope_response(
            status_code=exc.status_code,
            code=_http_error_code(exc.status_code),
            message=message,
            request_id=getattr(request.state, "request_id", None),
            details=details,
            headers=exc.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", path=request.url.path)
        HTTP_UNHANDLED_EXCEPTIONS_TOTAL.labels(
            path=_templated_path(request),
            exc_type=type(exc).__name__,
            pgcode=_pgcode(exc),
        ).inc()
        return envelope_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred",
            request_id=getattr(request.state, "request_id", None),
        )
