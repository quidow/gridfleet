"""Narrow Protocol definitions shared across domains.

Each protocol defines the minimal interface a consumer needs.
Concrete implementations satisfy these structurally (no inheritance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.type_defs import SettingValue


@runtime_checkable
class EmitProtocol(Protocol):
    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None: ...


@runtime_checkable
class SettingsReader(Protocol):
    def get(self, key: str) -> SettingValue: ...


@runtime_checkable
class SettingsWriter(Protocol):
    async def set(self, db: AsyncSession, key: str, value: str) -> None: ...
