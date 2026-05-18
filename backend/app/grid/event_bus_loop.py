"""Leader-owned loop that runs ``HubEventBusSubscriber``.

Lives outside ``session_sync_loop`` because the subscriber holds a
persistent socket; structurally it parallels the other ~17 leader-
owned loops in ``app/main.py``. On any unhandled crash the loop
restarts the subscriber after a short delay so a transient ZMQ error
does not wedge real-time session sync until the next process restart.
"""

from __future__ import annotations

import asyncio

from app.core.observability import get_logger, observe_background_loop
from app.grid import grid_settings
from app.grid.event_bus import DecodedEvent, HubEventBusSubscriber
from app.sessions.service_sync import wake_session_sync

logger = get_logger(__name__)
LOOP_NAME = "grid_event_bus_subscriber"
_RESTART_DELAY_SEC = 1.0
_HEARTBEAT_INTERVAL_SEC = 5.0


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
            async with observe_background_loop(LOOP_NAME, _HEARTBEAT_INTERVAL_SEC).cycle():
                # Park forever in a cancellable wait. The subscriber's
                # own receive task is doing the real work; we just need
                # this coroutine to stay alive so leader-loop shutdown
                # can cancel it cleanly.
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            await subscriber.stop()
            raise
        except Exception:  # supervisor restarts on any unhandled error
            logger.exception("grid_event_bus_subscriber_failed")
            await subscriber.stop()
            await asyncio.sleep(_RESTART_DELAY_SEC)
            continue
