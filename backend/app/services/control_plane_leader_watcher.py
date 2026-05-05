from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from app.config import freeze_background_loops_enabled
from app.database import engine as default_engine
from app.observability import get_logger, observe_background_loop
from app.services.control_plane_leader import ControlPlaneLeader, control_plane_leader
from app.services.settings_service import settings_service

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = get_logger(__name__)
LEADER_WATCHER_LOOP_NAME = "control_plane_leader_watcher"


async def _exit_after_preempt() -> None:
    """Exit after a successful watcher preempt."""
    logger.warning(
        "control_plane_leader_watcher_exiting_after_preempt",
        action="supervisor_restart_to_spawn_leader_loops",
    )
    os._exit(70)


async def run_watcher_once(
    leader: ControlPlaneLeader = control_plane_leader,
    *,
    engine: AsyncEngine | None = None,
) -> None:
    """One iteration. Visible to tests for direct drive."""
    if freeze_background_loops_enabled():
        return
    if leader._connection is not None:
        return
    if not settings_service.get("general.leader_keepalive_enabled"):
        return

    target_engine = engine or default_engine
    threshold = int(settings_service.get("general.leader_stale_threshold_sec"))
    try:
        acquired = await leader.try_acquire(target_engine, stale_threshold_sec=threshold)
    except Exception:
        logger.exception("control_plane_leader_watcher_failed")
        return

    if acquired:
        # Do not manually release here. os._exit drops the process sockets, and
        # Postgres releases the session advisory lock as part of connection cleanup.
        await _exit_after_preempt()


async def control_plane_leader_watcher_loop() -> None:
    """Always-on loop. Polls staleness and preempts when allowed."""
    while True:
        interval = float(settings_service.get("general.leader_keepalive_interval_sec"))
        try:
            async with observe_background_loop(LEADER_WATCHER_LOOP_NAME, interval).cycle():
                await run_watcher_once()
        except Exception:
            logger.exception("control_plane_leader_watcher_loop_failed")
        await asyncio.sleep(interval)
