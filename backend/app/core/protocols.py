"""Narrow Protocol definitions shared across domains.

Each protocol defines the minimal interface a consumer needs.
Concrete implementations satisfy these structurally (no inheritance).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class EmitProtocol(Protocol):
    async def __call__(self, event_type: str, payload: dict[str, Any]) -> None: ...


@runtime_checkable
class SettingsReader(Protocol):
    def get(self, key: str) -> str: ...

    def get_int(self, key: str) -> int: ...


@runtime_checkable
class SettingsWriter(Protocol):
    async def set(self, db: object, key: str, value: str) -> None: ...
