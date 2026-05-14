"""Backend mirror of the agent-side AgentErrorCode enum."""

from __future__ import annotations

import enum


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
