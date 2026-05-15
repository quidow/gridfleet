from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from agent_app.logs.handlers import ShipperHandler

if TYPE_CHECKING:
    from agent_app.logs.schemas import ShippedLogLine


@pytest.mark.asyncio
async def test_skips_heartbeat_logger() -> None:
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    handler = ShipperHandler(queue=queue, min_level=logging.INFO)
    record = logging.LogRecord(
        name="agent.heartbeat.request",
        level=logging.INFO,
        pathname="x",
        lineno=1,
        msg="hb",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    assert queue.empty()


@pytest.mark.asyncio
async def test_skips_below_min_level() -> None:
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    handler = ShipperHandler(queue=queue, min_level=logging.WARNING)
    record = logging.LogRecord(
        name="agent.foo",
        level=logging.INFO,
        pathname="x",
        lineno=1,
        msg="i",
        args=(),
        exc_info=None,
    )
    handler.emit(record)
    assert queue.empty()


@pytest.mark.asyncio
async def test_emits_monotonic_sequence() -> None:
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue()
    handler = ShipperHandler(queue=queue, min_level=logging.INFO)
    for i in range(3):
        record = logging.LogRecord(
            name="agent.foo",
            level=logging.INFO,
            pathname="x",
            lineno=1,
            msg=f"m{i}",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    lines = [queue.get_nowait() for _ in range(3)]
    assert [line.sequence_no for line in lines] == [0, 1, 2]


@pytest.mark.asyncio
async def test_drops_on_full_queue_and_counts() -> None:
    queue: asyncio.Queue[ShippedLogLine] = asyncio.Queue(maxsize=1)
    handler = ShipperHandler(queue=queue, min_level=logging.INFO)
    handler.set_min_level("INFO")
    for i in range(3):
        record = logging.LogRecord(
            name="agent.foo",
            level=logging.INFO,
            pathname="x",
            lineno=1,
            msg=f"m{i}",
            args=(),
            exc_info=None,
        )
        handler.emit(record)
    assert handler.dropped_count == 2
    assert queue.qsize() == 1
