"""Response schemas for ``/agent/tools/*``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ToolEntry(BaseModel):
    name: str
    version: str | None = None
    description: str


class ToolsStatusResponse(BaseModel):
    """Detected versions of supporting CLI tools, grouped by host and pack."""

    model_config = ConfigDict(extra="forbid")

    host: dict[str, ToolEntry]
    packs: dict[str, list[ToolEntry]]
