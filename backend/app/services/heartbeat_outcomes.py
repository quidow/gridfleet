from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class HeartbeatOutcome(StrEnum):
    success = "success"
    timeout = "timeout"
    connect_error = "connect_error"
    dns_error = "dns_error"
    http_error = "http_error"
    invalid_payload = "invalid_payload"
    circuit_open = "circuit_open"
    unexpected_error = "unexpected_error"


class ClientMode(StrEnum):
    pooled = "pooled"
    fresh = "fresh"
    skipped_circuit_open = "skipped_circuit_open"


@dataclass(frozen=True)
class HeartbeatPingResult:
    outcome: HeartbeatOutcome
    payload: dict[str, Any] | None
    duration_ms: int
    client_mode: ClientMode
    http_status: int | None
    error_category: str | None

    @property
    def alive(self) -> bool:
        return self.outcome is HeartbeatOutcome.success
