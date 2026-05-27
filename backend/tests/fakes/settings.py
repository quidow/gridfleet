"""Fake settings reader for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.type_defs import SettingValue


class FakeSettingsReader:
    """In-memory settings store for tests. Satisfies SettingsReader."""

    def __init__(self, overrides: dict[str, SettingValue] | None = None) -> None:
        self._data: dict[str, SettingValue] = overrides or {}

    def get(self, key: str) -> SettingValue:
        return self._data.get(key, "")

    def set(self, key: str, value: SettingValue) -> None:
        self._data[key] = value
