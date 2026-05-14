from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class SettingRead(BaseModel):
    key: str
    value: Any
    default_value: Any
    is_overridden: bool
    category: str
    description: str
    type: str
    validation: dict[str, Any] | None = None


class SettingUpdate(BaseModel):
    value: Any


class SettingsBulkUpdate(BaseModel):
    settings: dict[str, Any]


class SettingsGrouped(BaseModel):
    category: str
    display_name: str
    settings: list[SettingRead]
