"""Agent-side structured error codes."""

from __future__ import annotations

import enum
from typing import Any

from fastapi import HTTPException


class AgentErrorCode(enum.StrEnum):
    PORT_OCCUPIED = "PORT_OCCUPIED"
    ALREADY_RUNNING = "ALREADY_RUNNING"
    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    RUNTIME_MISSING = "RUNTIME_MISSING"
    STARTUP_TIMEOUT = "STARTUP_TIMEOUT"
    INVALID_PAYLOAD = "INVALID_PAYLOAD"
    PROBE_FAILED = "PROBE_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


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
