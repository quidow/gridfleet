"""Plugin-sync DTOs."""

from __future__ import annotations

from pydantic import BaseModel, Field

_PLUGIN_NAME_PATTERN = r"^(@[a-z0-9][a-z0-9_\-]*/)?[a-z0-9][a-z0-9_.\-]*$"


class PluginConfig(BaseModel):
    name: str = Field(min_length=1, pattern=_PLUGIN_NAME_PATTERN)
    version: str
    source: str
    package: str | None = None


class PluginSyncRequest(BaseModel):
    plugins: list[PluginConfig]
