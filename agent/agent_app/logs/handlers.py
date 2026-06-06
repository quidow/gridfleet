"""Log handlers attached to the agent root logger."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from itertools import count

from agent_app.logs.schemas import ShippedLogLine

_HEARTBEAT_LOGGER_PREFIXES = ("agent.heartbeat.request",)


class ShipperHandler(logging.Handler):
    """Queues log records as ShippedLogLine entries for the LogShipperTask."""

    def __init__(self, *, queue: asyncio.Queue[ShippedLogLine], min_level: int = logging.INFO) -> None:
        super().__init__()
        self._queue = queue
        self._min_level = min_level
        self._seq = count()
        self.dropped_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(_HEARTBEAT_LOGGER_PREFIXES):
            return
        if record.levelno < self._min_level:
            return
        try:
            rendered = record.getMessage()
        except Exception:
            self.handleError(record)
            return
        line = ShippedLogLine(
            ts=datetime.fromtimestamp(record.created, tz=UTC),
            level=record.levelname,
            logger_name=record.name,
            message=rendered,
            sequence_no=next(self._seq),
        )
        try:
            self._queue.put_nowait(line)
        except asyncio.QueueFull:
            self.dropped_count += 1
