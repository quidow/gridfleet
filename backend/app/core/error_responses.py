"""Canonical OpenAPI error responses for documentation polish."""

from typing import Any

from pydantic import BaseModel


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str | None
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


RESPONSES_400: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorResponse, "description": "Validation error"},
}
RESPONSES_401: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Authentication required"},
}
RESPONSES_403: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Forbidden"},
}
RESPONSES_404: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "Resource not found"},
}
RESPONSES_409: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "State conflict"},
}
RESPONSES_422: dict[int | str, dict[str, Any]] = {
    422: {"model": ErrorResponse, "description": "Request body validation failed"},
}
