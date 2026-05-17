"""Pydantic schemas for the device diagnostic export feature."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

DIAGNOSTIC_BUNDLE_SCHEMA_VERSION = 1


class DiagnosticSnapshotSummary(BaseModel):
    """One row in the snapshot history list."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    captured_at: datetime
    trigger: str
    reason: str | None = None


class DiagnosticSnapshotDetail(BaseModel):
    """One snapshot, including the bundle payload."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    captured_at: datetime
    trigger: str
    reason: str | None = None
    payload: dict[str, Any]


class DiagnosticExportResponse(BaseModel):
    """Response from POST /devices/{id}/diagnostics/export."""

    payload: dict[str, Any]
    snapshot_id: uuid.UUID | None = None
    warnings: list[str] = Field(default_factory=list)


class DiagnosticSnapshotListResponse(BaseModel):
    items: list[DiagnosticSnapshotSummary]
    next_before: uuid.UUID | None = None


TriggerLiteral = Literal["operator", "review_required"]
