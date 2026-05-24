"""Backend subscriber for the Selenium Grid hub event bus.

This module is the backend twin of ``agent_app/grid_node/event_bus.py``.
Both decode the same four-frame wire format produced by Selenium's
``UnboundZmqEventBus``: ``[event-name, secret, event-id, data]``. Keep
the decoders in sync — any wire-format change must touch both files.

The subscriber class lives in this module too (added in Task 7); only
the decoder is exposed here alongside the subscriber.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import zmq
import zmq.asyncio
from prometheus_client import Counter, Gauge

if TYPE_CHECKING:
    from collections.abc import Callable

GRID_EVENT_BUS_CONNECTED = Gauge(
    "gridfleet_grid_event_bus_connected",
    "1 = subscriber connected to hub event bus, 0 = disconnected / connecting.",
)
GRID_EVENT_BUS_EVENTS_RECEIVED_TOTAL = Counter(
    "gridfleet_grid_event_bus_events_received",
    "Total event-bus frames received, by event type.",
    labelnames=("event_type",),
)
GRID_EVENT_BUS_DECODE_FAILURES_TOTAL = Counter(
    "gridfleet_grid_event_bus_decode_failures",
    "Total frames discarded due to decode failure.",
)
GRID_EVENT_BUS_LAST_EVENT_AGE_SECONDS = Gauge(
    "gridfleet_grid_event_bus_last_event_age_seconds",
    "Seconds since the last well-formed event was received. NaN until first event.",
)


@dataclass(frozen=True)
class DecodedEvent:
    type: str
    data: Any


def decode_event_frames(frames: list[bytes]) -> DecodedEvent:
    """Decode Selenium 4 event-bus frames into a ``DecodedEvent``.

    Raises ``ValueError`` on any structural problem. The subscriber loop
    catches the error, counts it (``grid_event_bus_decode_failures_total``),
    and continues — a malformed frame must not break the subscriber.
    """
    if len(frames) < 4:
        raise ValueError(f"expected 4 grid event-bus frames, got {len(frames)}")
    try:
        event_type = frames[0].decode("utf-8")
        data = json.loads(frames[3].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed grid event-bus frames") from exc
    if not event_type:
        raise ValueError("missing event type frame")
    return DecodedEvent(type=event_type, data=data)


logger = logging.getLogger(__name__)

# Selenium hub emits many event types; the subscriber forwards only
# the ones session_sync_loop reacts to. Other types still count toward
# events_received (bus-liveness signal) but do not invoke on_event.
_ACTIONABLE_EVENT_TYPES: frozenset[str] = frozenset({"session-created", "session-closed"})


@dataclass
class SubscriberMetrics:
    events_received: dict[str, int] = field(default_factory=dict)
    decode_failures: int = 0
    last_event_received_at: float | None = None

    def record_event(self, event_type: str) -> None:
        self.events_received[event_type] = self.events_received.get(event_type, 0) + 1
        self.last_event_received_at = time.monotonic()
        GRID_EVENT_BUS_EVENTS_RECEIVED_TOTAL.labels(event_type=event_type).inc()


class HubEventBusSubscriber:
    """Subscribes to the Selenium hub ZMQ event bus.

    Read-only against the bus. Every actionable event invokes
    ``on_event``; all state mutation happens in the consumer (the
    session-sync doorbell handler) so this class never touches the DB.
    """

    def __init__(
        self,
        *,
        subscribe_url: str,
        on_event: Callable[[DecodedEvent], None],
    ) -> None:
        self._subscribe_url = subscribe_url
        self._on_event = on_event
        self._context = zmq.asyncio.Context.instance()
        self._socket: zmq.asyncio.Socket | None = None
        self._task: asyncio.Task[None] | None = None
        self.metrics = SubscriberMetrics()

    async def start(self) -> None:
        if self._socket is not None or self._task is not None:
            return
        self._socket = self._context.socket(zmq.SUB)
        self._socket.setsockopt(zmq.SUBSCRIBE, b"")
        self._socket.connect(self._subscribe_url)
        self._task = asyncio.create_task(self._receive_loop(), name="grid_event_bus_subscriber")
        GRID_EVENT_BUS_CONNECTED.set(1)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await self._task
                except Exception:  # noqa: BLE001
                    logger.warning("grid event bus subscriber task failed during shutdown", exc_info=True)
            self._task = None
        if self._socket is not None:
            self._socket.close(linger=0)
            self._socket = None
        GRID_EVENT_BUS_CONNECTED.set(0)

    async def _receive_loop(self) -> None:
        socket = self._socket
        if socket is None:
            return
        while True:
            frames = await socket.recv_multipart()
            try:
                event = decode_event_frames(list(frames))
            except ValueError:
                # Malformed frame; demoted to debug because stray TCP probes hit
                # the XPUB port in dev. Counter exposes real malformed traffic.
                self.metrics.decode_failures += 1
                GRID_EVENT_BUS_DECODE_FAILURES_TOTAL.inc()
                logger.debug("discarding malformed grid event bus frames", exc_info=True)
                continue
            self.metrics.record_event(event.type)
            if event.type not in _ACTIONABLE_EVENT_TYPES:
                continue
            try:
                self._on_event(event)
            except Exception:  # noqa: BLE001
                logger.warning("grid event bus handler failed", exc_info=True)
