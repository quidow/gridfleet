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
from typing import TYPE_CHECKING

from app.core.observability import get_logger, observe_background_loop
from app.grid import grid_settings
from app.grid.event_bus import (
    GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS,
    NODE_EVENT_TYPES,
    DecodedEvent,
    HubEventBusSubscriber,
    SubscriberMetrics,
)

if TYPE_CHECKING:
    from app.grid.protocols import NodeHealthWaker, SessionSyncWaker
    from app.grid.services_container import GridServices

logger = get_logger(__name__)
LOOP_NAME = "grid_event_bus_subscriber"
_RESTART_DELAY_SEC = 1.0
_HEARTBEAT_INTERVAL_SEC = 5.0


class GridEventBusSubscriberLoop:
    def __init__(
        self,
        *,
        services: GridServices,
        session_sync_waker: SessionSyncWaker,
        node_health_waker: NodeHealthWaker,
    ) -> None:
        self._services = services
        self._session_sync_waker = session_sync_waker
        self._node_health_waker = node_health_waker

    def _handle_event(self, event: DecodedEvent) -> None:
        # Node events mean device availability changed now; a removed node
        # also takes its sessions with it without emitting session-closed,
        # so every actionable event wakes session_sync_loop too.
        if event.type in NODE_EVENT_TYPES:
            self._node_health_waker.wake()
        self._session_sync_waker.wake()
        logger.debug("grid_event_bus_event", type=event.type)

    async def run(self) -> None:
        while True:
            subscriber = HubEventBusSubscriber(
                subscribe_url=grid_settings.event_bus_subscribe_url,
                on_event=self._handle_event,
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


async def _refresh_last_event_age(metrics: SubscriberMetrics) -> None:
    while True:
        if metrics.last_event_received_at is not None:
            GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS.set(time.monotonic() - metrics.last_event_received_at)
        await asyncio.sleep(1.0)
