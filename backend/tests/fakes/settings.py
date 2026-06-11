"""Fake settings reader for tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.settings.registry import SETTINGS_REGISTRY, resolve_default

if TYPE_CHECKING:
    from app.core.type_defs import SettingValue


class FakeSettingsReader:
    """In-memory settings store for tests. Satisfies SettingsReader."""

    def __init__(self, overrides: dict[str, SettingValue] | None = None) -> None:
        self._data: dict[str, SettingValue] = overrides or {}

    def get(self, key: str) -> SettingValue:
        if key in self._data:
            return self._data[key]
        if key in SETTINGS_REGISTRY:
            return resolve_default(SETTINGS_REGISTRY[key])
        return ""

    def get_int(self, key: str) -> int:
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"Setting {key} is not an int: {value!r}")
        return value

    def get_float(self, key: str) -> float:
        value = self.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"Setting {key} is not a float: {value!r}")
        return float(value)

    def get_bool(self, key: str) -> bool:
        value = self.get(key)
        if not isinstance(value, bool):
            raise TypeError(f"Setting {key} is not a bool: {value!r}")
        return value

    def set(self, key: str, value: SettingValue) -> None:
        self._data[key] = value
