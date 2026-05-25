"""Fake settings reader for tests."""

from __future__ import annotations


class FakeSettingsReader:
    """In-memory settings store for tests. Satisfies SettingsReader."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = overrides or {}

    def get(self, key: str) -> str:
        return self._data.get(key, "")

    def get_int(self, key: str) -> int:
        raw = self._data.get(key, "0")
        return int(raw)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
