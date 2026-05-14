"""Typed projection of agent status responses into a tri-state result."""

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
