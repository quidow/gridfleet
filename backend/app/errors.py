from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.observability import get_logger
from app.services.node_manager_types import NodeManagerError

logger = get_logger(__name__)


class AgentCallError(Exception):
    error_code = "AGENT_UNREACHABLE"
    status_code = 502

    def __init__(self, host: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.host = host
        self.message = message
        self.details = details or {"host": host}


class AgentUnreachableError(AgentCallError):
    error_code = "AGENT_UNREACHABLE"
    status_code = 502


class CircuitOpenError(AgentCallError):
    error_code = "CIRCUIT_OPEN"
    status_code = 503

    def __init__(self, host: str, *, retry_after_seconds: float | None = None) -> None:
        details: dict[str, Any] = {"host": host}
        if retry_after_seconds is not None:
            details["retry_after_seconds"] = round(retry_after_seconds, 3)
        super().__init__(host, f"Host {host} is temporarily unreachable", details=details)


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


def request_id_from_scope(scope: Mapping[str, Any]) -> str | None:
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    request_id = state.get("request_id")
    return request_id if isinstance(request_id, str) and request_id else None


def request_id_from_request(request: Request) -> str | None:
    request_id = getattr(request.state, "request_id", None)
    return request_id if isinstance(request_id, str) and request_id else None


def build_error_body(
    *,
    code: str,
    message: str,
    request_id: str | None,
    details: object | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
        }
    }
    if details is not None:
        payload["error"]["details"] = jsonable_encoder(details)
    return payload


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str | None,
    details: object | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=build_error_body(code=code, message=message, request_id=request_id, details=details),
    )


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


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AgentCallError)
    async def handle_agent_call_error(request: Request, exc: AgentCallError) -> JSONResponse:
        return error_response(
            status_code=exc.status_code,
            code=exc.error_code,
            message=exc.message,
            request_id=request_id_from_request(request),
            details=exc.details,
        )

    @app.exception_handler(NodeManagerError)
    async def handle_node_manager_error(request: Request, exc: NodeManagerError) -> JSONResponse:
        return error_response(
            status_code=400,
            code="VALIDATION_ERROR",
            message=str(exc),
            request_id=request_id_from_request(request),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            status_code=422,
            code="VALIDATION_ERROR",
            message="Request validation failed",
            request_id=request_id_from_request(request),
            details=exc.errors(),
        )

    @app.exception_handler(HTTPException)
    async def handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        message, details = _http_error_payload(exc)
        return error_response(
            status_code=exc.status_code,
            code=_http_error_code(exc.status_code),
            message=message,
            request_id=request_id_from_request(request),
            details=details,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", path=request.url.path)
        return error_response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="An unexpected error occurred",
            request_id=request_id_from_request(request),
        )
