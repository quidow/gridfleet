"""Narrow Protocol definitions shared across domains.

Each protocol defines the minimal interface a consumer needs.
Concrete implementations satisfy these structurally (no inheritance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.core.type_defs import SettingValue


class SettingsReader(Protocol):
    def get(self, key: str) -> SettingValue: ...
    def get_int(self, key: str) -> int: ...
    def get_float(self, key: str) -> float: ...
    def get_bool(self, key: str) -> bool: ...
