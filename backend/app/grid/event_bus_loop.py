"""Leader-owned loop that runs ``HubEventBusSubscriber``.

Lives outside ``session_sync_loop`` because the subscriber holds a
persistent socket; structurally it parallels the other ~17 leader-
owned loops in ``app/main.py``. On any unhandled crash the loop
restarts the subscriber after a short delay so a transient ZMQ error
does not wedge real-time session sync until the next process restart.
"""

from __future__ import annotations

import asyncio
import contextlib
import time

from app.core.observability import get_logger, observe_background_loop
from app.grid import grid_settings
from app.grid.event_bus import (
    GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS,
    DecodedEvent,
    HubEventBusSubscriber,
    SubscriberMetrics,
)
from app.sessions.service_sync import wake_session_sync

logger = get_logger(__name__)
LOOP_NAME = "grid_event_bus_subscriber"
_RESTART_DELAY_SEC = 1.0
_HEARTBEAT_INTERVAL_SEC = 5.0


async def _refresh_last_event_age(metrics: SubscriberMetrics) -> None:
    while True:
        if metrics.last_event_received_at is not None:
            GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS.set(time.monotonic() - metrics.last_event_received_at)
        await asyncio.sleep(1.0)


def _handle_event(event: DecodedEvent) -> None:
    # Subscriber only forwards session-created / session-closed.
    # Either one means session_sync_loop should look at the hub now.
    wake_session_sync()
    logger.debug("grid_event_bus_event", type=event.type)


async def event_bus_subscriber_loop() -> None:
    while True:
        subscriber = HubEventBusSubscriber(
            subscribe_url=grid_settings.event_bus_subscribe_url,
            on_event=_handle_event,
        )
        try:
            await subscriber.start()
            refresher = asyncio.create_task(
                _refresh_last_event_age(subscriber.metrics), name="grid_event_bus_age_refresher"
            )
            try:
                while True:
                    async with observe_background_loop(LOOP_NAME, _HEARTBEAT_INTERVAL_SEC).cycle():
                        # Subscriber receive task is the real worker; the
                        # supervisor just ticks the heartbeat so the metric
                        # stays warm and exposes "is the supervisor alive".
                        pass
                    await asyncio.sleep(_HEARTBEAT_INTERVAL_SEC)
            finally:
                refresher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await refresher
        except asyncio.CancelledError:
            await subscriber.stop()
            raise
        except Exception:  # supervisor restarts on any unhandled error
            logger.exception("grid_event_bus_subscriber_failed")
            await subscriber.stop()
            await asyncio.sleep(_RESTART_DELAY_SEC)
            continue
