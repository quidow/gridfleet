from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from app.core.observability import get_logger, observe_background_loop

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.core.leader.advisory import ControlPlaneLeader
    from app.core.protocols import SettingsReader

logger = get_logger(__name__)
LEADER_WATCHER_LOOP_NAME = "control_plane_leader_watcher"


class LeaderWatcherLoop:
    def __init__(self, *, settings: SettingsReader, leader: ControlPlaneLeader, engine: AsyncEngine) -> None:
        self._settings = settings
        self._leader = leader
        self._engine = engine

    async def run(self) -> None:
        """Always-on loop. Polls staleness and preempts when allowed."""
        while True:
            interval = float(self._settings.get("general.leader_keepalive_interval_sec"))
            try:
                async with observe_background_loop(LEADER_WATCHER_LOOP_NAME, interval).cycle():
                    await run_watcher_once(self._leader, engine=self._engine, settings=self._settings)
            except Exception:
                logger.exception("control_plane_leader_watcher_loop_failed")
            await asyncio.sleep(interval)


async def _exit_after_preempt() -> None:
    """Exit after a successful watcher preempt."""
    logger.warning(
        "control_plane_leader_watcher_exiting_after_preempt",
        action="supervisor_restart_to_spawn_leader_loops",
    )
    os._exit(70)


async def run_watcher_once(
    leader: ControlPlaneLeader,
    *,
    engine: AsyncEngine,
    settings: SettingsReader,
) -> None:
    """One iteration. Visible to tests for direct drive."""
    if leader._connection is not None:
        return
    if not settings.get("general.leader_keepalive_enabled"):
        return

    threshold = int(settings.get("general.leader_stale_threshold_sec"))
    try:
        acquired = await leader.try_acquire(engine, stale_threshold_sec=threshold)
    except Exception:
        logger.exception("control_plane_leader_watcher_failed")
        return

    if acquired:
        # Do not manually release here. os._exit drops the process sockets, and
        # Postgres releases the session advisory lock as part of connection cleanup.
        await _exit_after_preempt()
