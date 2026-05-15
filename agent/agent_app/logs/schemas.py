"""Pydantic models for the agent log shipper."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - Pydantic resolves this field annotation at runtime.
from uuid import UUID  # noqa: TC003 - Pydantic resolves this field annotation at runtime.

from pydantic import BaseModel


class ShippedLogLine(BaseModel):
    ts: datetime
    level: str
    logger_name: str
    message: str
    sequence_no: int


class AgentLogBatch(BaseModel):
    boot_id: UUID
    lines: list[ShippedLogLine]
