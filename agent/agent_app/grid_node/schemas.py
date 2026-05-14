"""Grid-node DTOs."""

from __future__ import annotations

from uuid import UUID  # noqa: TC003 - Pydantic resolves at runtime

from pydantic import BaseModel


class GridNodeReregisterRequest(BaseModel):
    target_run_id: UUID | None = None


class GridNodeReregisterResponse(BaseModel):
    grid_run_id: UUID | None
