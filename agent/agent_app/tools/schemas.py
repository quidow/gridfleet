"""Response schemas for ``/agent/tools/*``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ToolsStatusResponse(BaseModel):
    """Detected versions of supporting CLI tools. Keys vary by host."""

    model_config = ConfigDict(extra="allow")
