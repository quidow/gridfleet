import asyncio

from app.database import async_session
from app.observability import get_logger, observe_background_loop, schedule_background_loop
from app.services import appium_node_resource_service
from app.services.settings_service import settings_service

logger = get_logger(__name__)
LOOP_NAME = "appium_resource_sweeper"


async def appium_resource_sweeper_loop() -> None:
    """Reap expired temporary Appium resource claims."""
    interval = float(settings_service.get("appium.resource_sweeper_interval_sec"))
    await schedule_background_loop(LOOP_NAME, interval)
    while True:
        await asyncio.sleep(interval)
        try:
            async with observe_background_loop(LOOP_NAME, interval).cycle(), async_session() as db:
                swept = await appium_node_resource_service.sweep_expired(db)
                await db.commit()
                if swept:
                    logger.info("Reaped %d expired temporary Appium resource claims", swept)
        except Exception:
            logger.exception("Appium resource sweeper failed")
        interval = float(settings_service.get("appium.resource_sweeper_interval_sec"))
