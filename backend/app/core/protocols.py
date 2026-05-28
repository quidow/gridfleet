"""Narrow Protocol definitions shared across domains.

Each protocol defines the minimal interface a consumer needs.
Concrete implementations satisfy these structurally (no inheritance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.core.type_defs import SettingValue


@runtime_checkable
class SettingsReader(Protocol):
    def get(self, key: str) -> SettingValue: ...
