"""Plugin-sync DTOs."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PluginConfig(BaseModel):
    name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_./\-@]+$")
    version: str
    source: str
    package: str | None = None


class PluginSyncRequest(BaseModel):
    plugins: list[PluginConfig]
