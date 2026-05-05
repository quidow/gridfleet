from __future__ import annotations

import asyncio

from app.observability import get_logger, observe_background_loop
from app.services.control_plane_leader import LeadershipLost, control_plane_leader
from app.services.settings_service import settings_service

logger = get_logger(__name__)
LEADER_KEEPALIVE_LOOP_NAME = "control_plane_leader_keepalive"


async def run_keepalive_once() -> None:
    """One iteration. Extracted so tests can drive it without sleeping."""
    if not settings_service.get("general.leader_keepalive_enabled"):
        return
    try:
        await control_plane_leader.write_heartbeat()
    except LeadershipLost:
        raise
    except Exception:
        logger.exception("control_plane_leader_keepalive_failed")


async def control_plane_leader_keepalive_loop() -> None:
    while True:
        interval = float(settings_service.get("general.leader_keepalive_interval_sec"))
        try:
            async with observe_background_loop(LEADER_KEEPALIVE_LOOP_NAME, interval).cycle():
                await run_keepalive_once()
        except LeadershipLost:
            logger.error("control_plane_leader_lost", action="exiting_process_to_prevent_split_brain")
            # We are inside a background task after the advisory-lock connection
            # failed or another holder claimed the row. sys.exit() can be
            # intercepted by framework code; os._exit(70) guarantees the
            # supervisor gets a clean process failure to restart.
            import os

            os._exit(70)
        await asyncio.sleep(interval)
