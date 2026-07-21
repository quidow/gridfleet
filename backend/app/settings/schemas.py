from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.core.type_defs import SettingType


class SettingValidation(BaseModel):
    min: float | None = None
    max: float | None = None
    allowed_values: list[str] | None = None
    item_type: str | None = None
    item_allowed_values: list[str] | None = None


class SettingRead(BaseModel):
    key: str
    value: Any
    default_value: Any
    is_overridden: bool
    category: str
    description: str
    type: SettingType
    validation: SettingValidation | None = None


class SettingUpdate(BaseModel):
    value: Any


class SettingsBulkUpdate(BaseModel):
    settings: dict[str, Any]


class SettingsGrouped(BaseModel):
    category: str
    display_name: str
    settings: list[SettingRead]
