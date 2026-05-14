"""Agent-side structured error codes."""

from __future__ import annotations

import enum
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel


class AgentErrorCode(enum.StrEnum):
    PORT_OCCUPIED = "PORT_OCCUPIED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    NO_ADAPTER = "NO_ADAPTER"
    RUNTIME_MISSING = "RUNTIME_MISSING"
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNKNOWN_PLATFORM = "UNKNOWN_PLATFORM"


class ErrorEnvelopeDetail(BaseModel):
    code: str
    message: str


class ErrorEnvelope(BaseModel):
    detail: ErrorEnvelopeDetail


def http_exc(
    *,
    status_code: int,
    code: AgentErrorCode,
    message: str,
    extra: dict[str, Any] | None = None,
) -> HTTPException:
    payload: dict[str, Any] = {"code": code.value, "message": message}
    if extra:
        payload.update(extra)
    return HTTPException(status_code=status_code, detail=payload)
