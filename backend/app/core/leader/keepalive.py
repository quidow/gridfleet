from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from app.core.leader.advisory import LeadershipLost, control_plane_leader
from app.core.observability import get_logger, observe_background_loop

if TYPE_CHECKING:
    from app.core.protocols import SettingsReader

logger = get_logger(__name__)
LEADER_KEEPALIVE_LOOP_NAME = "control_plane_leader_keepalive"


class LeaderKeepaliveLoop:
    def __init__(self, *, settings: SettingsReader) -> None:
        self._settings = settings

    async def run(self) -> None:
        while True:
            interval = float(self._settings.get("general.leader_keepalive_interval_sec"))
            try:
                async with observe_background_loop(LEADER_KEEPALIVE_LOOP_NAME, interval).cycle():
                    await run_keepalive_once(settings=self._settings)
            except LeadershipLost:
                logger.error("control_plane_leader_lost", action="exiting_process_to_prevent_split_brain")
                # We are inside a background task after the advisory-lock connection
                # failed or another holder claimed the row. sys.exit() can be
                # intercepted by framework code; os._exit(70) guarantees the
                # supervisor gets a clean process failure to restart.
                os._exit(70)
            await asyncio.sleep(interval)


async def run_keepalive_once(*, settings: SettingsReader) -> None:
    """One iteration. Extracted so tests can drive it without sleeping."""
    if not settings.get("general.leader_keepalive_enabled"):
        return
    try:
        await control_plane_leader.write_heartbeat()
    except LeadershipLost:
        raise
    except Exception:
        logger.exception("control_plane_leader_keepalive_failed")
