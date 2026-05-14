from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LiveHealthRead(BaseModel):
    status: str


class HealthStatusRead(BaseModel):
    status: str
    # Checks include dynamic loop names and operational details; keep the map flexible.
    checks: dict[str, Any] = Field(default_factory=dict)
