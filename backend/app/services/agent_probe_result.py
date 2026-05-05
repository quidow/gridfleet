"""Typed projection of agent probe responses into a tri-state result."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True)
class ProbeResult:
    status: Literal["ack", "refused", "indeterminate"]
    detail: str | None = None


def from_status_response(payload: dict[str, Any] | None) -> ProbeResult:
    if payload is None:
        return ProbeResult(status="indeterminate", detail="agent unreachable or non-2xx")
    if payload.get("running") is True:
        return ProbeResult(status="ack")
    return ProbeResult(status="refused", detail="Appium not running")


def from_probe_session_response(result: tuple[bool, str | None]) -> ProbeResult:
    healthy, error = result
    if healthy:
        return ProbeResult(status="ack")
    if isinstance(error, str) and error.startswith("Probe session failed (HTTP "):
        return ProbeResult(status="indeterminate", detail=error)
    return ProbeResult(status="refused", detail=error)
