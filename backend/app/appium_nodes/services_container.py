"""Appium node domain service container."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.appium_nodes.protocols import ReconcilerProtocol
    from app.appium_nodes.services.heartbeat import HeartbeatService
    from app.appium_nodes.services.node_health import NodeHealthService
    from app.appium_nodes.services.reconciler_agent import ReconcilerAgentService
    from app.core.protocols import SettingsReader


@dataclass(frozen=True, slots=True)
class AppiumNodeServices:
    reconciler: ReconcilerProtocol
    reconciler_agent: ReconcilerAgentService
    node_health: NodeHealthService
    heartbeat: HeartbeatService
    settings: SettingsReader
    session_factory: async_sessionmaker[AsyncSession]
