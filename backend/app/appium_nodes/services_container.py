"""Appium node domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.appium_nodes.protocols import HeartbeatProtocol, NodeHealthProtocol, ReconcilerProtocol
    from app.core.protocols import SettingsReader


@dataclass(frozen=True, slots=True)
class AppiumNodeServices:
    reconciler: ReconcilerProtocol
    node_health: NodeHealthProtocol
    heartbeat: HeartbeatProtocol
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
