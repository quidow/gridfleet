"""Appium-node domain Protocol definitions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@runtime_checkable
class ReconcilerProtocol(Protocol):
    async def run_cycle(self, db: AsyncSession) -> None: ...


@runtime_checkable
class NodeHealthProtocol(Protocol):
    async def check_nodes(self, db: AsyncSession) -> None: ...


@runtime_checkable
class HeartbeatProtocol(Protocol):
    async def run_cycle(self, db: AsyncSession) -> None: ...
