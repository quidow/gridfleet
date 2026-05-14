"""Plugin-sync DTOs."""

from __future__ import annotations

from pydantic import BaseModel


class PluginConfig(BaseModel):
    name: str
    version: str
    source: str
    package: str | None = None


class PluginSyncRequest(BaseModel):
    plugins: list[PluginConfig]
